"""
TikTok Content Posting API publisher. Uses FILE_UPLOAD transfer (not PULL_FROM_URL,
which requires domain ownership verification). Videos land in the creator's draft
inbox; a human taps publish.

Scope required: video.upload (not video.publish — that skips the review inbox).
Rate limit: 6 requests/minute per user token.

Access tokens expire in 24h. Rather than wait for a 401, the token is refreshed
proactively (~1h before expiry) and persisted in the DB (src.db tokens table) —
expired tokens are the most common way these pipelines die silently.

Gate: if no usable token (env or DB) is configured, raise CredentialsMissing.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from src.db import get_token, set_token

logger = logging.getLogger(__name__)

API_BASE = "https://open.tiktokapis.com/v2"
CHUNK_SIZE = 10 * 1024 * 1024   # 10 MB per chunk (TikTok minimum is 5 MB, max 64 MB)
REFRESH_MARGIN = timedelta(hours=1)   # refresh proactively this far before expiry

# Status-fetch outcomes that mean assembly produced non-conforming output —
# these should be logged loudly and not retried blindly, per spec.
FATAL_CHECK_FAILURES = {
    "file_format_check_failed",
    "duration_check_failed",
    "frame_rate_check_failed",
}
TERMINAL_STATUSES = {"PUBLISH_COMPLETE", "FAILED"} | FATAL_CHECK_FAILURES
POLL_INTERVAL = 5
POLL_TIMEOUT = 60   # inbox drafts can take longer to fully settle than this; a
                     # timeout here is logged, not fatal — the draft is already
                     # uploaded and will appear in the inbox regardless.


class CredentialsMissing(Exception):
    pass


def _check_credentials(cfg):
    if not cfg.tiktok_access_token and not cfg.tiktok_refresh_token:
        raise CredentialsMissing(
            "TikTok credentials are not configured. "
            "Set TIKTOK_ACCESS_TOKEN (or TIKTOK_CLIENT_KEY/SECRET/REFRESH_TOKEN) in .env."
        )


def _ensure_fresh_token(cfg, conn) -> str:
    """
    Return a valid access token, refreshing proactively if the stored one is
    missing or within REFRESH_MARGIN of expiry. First call after adding a fresh
    env token has no stored expiry, so it refreshes immediately to establish one.
    """
    row = get_token(conn, "tiktok")
    now = datetime.now(timezone.utc)

    if row is not None:
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at - now > REFRESH_MARGIN:
            return row["access_token"]
        logger.info("TikTok token expires at %s — refreshing proactively", expires_at)

    if not (cfg.tiktok_client_key and cfg.tiktok_client_secret):
        if row is not None:
            return row["access_token"]   # can't refresh without app creds — use what we have
        raise CredentialsMissing(
            "No stored TikTok token and TIKTOK_CLIENT_KEY/SECRET are not set — cannot refresh."
        )

    refresh_tok = (row["refresh_token"] if row else None) or cfg.tiktok_refresh_token
    if not refresh_tok:
        if cfg.tiktok_access_token:
            return cfg.tiktok_access_token   # no refresh token available — use env token as-is
        raise CredentialsMissing("No TikTok refresh token available to obtain an access token.")

    data = _refresh_token(cfg, refresh_tok)
    expires_at = (now + timedelta(seconds=data["expires_in"])).isoformat()
    set_token(conn, "tiktok", data["access_token"], expires_at, refresh_token=data.get("refresh_token"))
    logger.info("TikTok token refreshed, expires at %s", expires_at)
    return data["access_token"]


def query_creator_info(token: str) -> dict:
    """Mandatory pre-flight for Direct Post per TikTok's content-sharing
    guidelines — returns privacy_level_options (what this creator/app pairing
    is actually allowed to post as) plus duet/comment/stitch defaults. Posting
    a privacy_level not present in this list is rejected."""
    resp = requests.post(
        f"{API_BASE}/post/publish/creator_info/query/",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("data", {})


def upload_draft(mp4_path: Path, caption: str, cfg, conn=None, cover_ms: int = 1000) -> str:
    """
    Publish a video to TikTok. Returns the publish_id. Dispatches on
    cfg.tiktok.post_mode:
      - "direct" (default): caption/hashtags apply automatically, video goes
        live immediately at whatever privacy_level TikTok allows (SELF_ONLY
        while this app is unaudited — the account owner can manually flip an
        individual post's privacy to "Everyone" afterward; this does NOT
        require completing TikTok's app review, confirmed working 2026-09-02).
      - "inbox": human finishes posting in the TikTok app. The API-sent
        caption is structurally ignored by this flow — confirmed against
        TikTok's own design (independent third-party tools hit the same wall),
        not fixable in code. Kept for reference/rollback.

    cover_ms: millisecond offset used as the cover frame — a mid-animation
    frame looks broken as a still, so this is computed from the actual render
    timing (src.cli._cover_ms_for) rather than left fixed.
    """
    _check_credentials(cfg)
    token = _ensure_fresh_token(cfg, conn) if conn is not None else cfg.tiktok_access_token
    file_size = mp4_path.stat().st_size
    chunk_size, total_chunks = _chunk_plan(file_size)
    mode = cfg.tiktok.post_mode

    if mode == "direct":
        creator_info = query_creator_info(token)
        privacy_options = creator_info.get("privacy_level_options", ["SELF_ONLY"])
        # Prefer full public visibility — contrary to docs describing unaudited
        # apps as forced to SELF_ONLY, PUBLIC_TO_EVERYONE has been observed
        # available in privacy_level_options for this app (confirmed live,
        # 2026-09-02). Fall back gracefully if it's ever not offered.
        privacy_level = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in privacy_options else privacy_options[0]
        init_path = "post/publish/video/init/"
        post_info = {
            "title": caption[:2200],   # Direct Post caption limit is 2,200 chars, not 150
            "privacy_level": privacy_level,
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": cover_ms,
        }
        logger.info("Direct-posting to TikTok at privacy_level=%s", privacy_level)
    else:
        init_path = "post/publish/inbox/video/init/"
        post_info = {
            "title": caption[:150],   # TikTok title max 150 chars
            "privacy_level": "SELF_ONLY",   # draft — human publishes
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": cover_ms,
        }

    # Step 1 — initialize upload
    logger.info("Initializing TikTok upload for %s (%d bytes, %d chunk(s)) …",
                mp4_path.name, file_size, total_chunks)
    init_resp = requests.post(
        f"{API_BASE}/{init_path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "post_info": post_info,
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunks,
            },
        },
        timeout=30,
    )
    if init_resp.status_code != 200:
        raise RuntimeError(f"TikTok upload init failed ({init_resp.status_code}): {init_resp.text[:300]}")
    data = init_resp.json().get("data", {})
    publish_id = data["publish_id"]
    upload_url = data["upload_url"]
    logger.info("Upload initialized: publish_id=%s", publish_id)

    # Step 2 — upload chunks
    _upload_chunks(mp4_path, upload_url, file_size, chunk_size, total_chunks, token)
    logger.info("TikTok video uploaded: publish_id=%s", publish_id)

    # Step 3 — poll status; surface conformance failures loudly rather than
    # retrying blindly, since they mean assembly produced a bad file, not a
    # transient network issue.
    status = poll_status(publish_id, token)
    if status in FATAL_CHECK_FAILURES:
        raise RuntimeError(
            f"TikTok rejected the upload: {status}. This means the assembled MP4 "
            f"doesn't conform (codec/duration/frame-rate) — check src/assemble/build.py "
            f"output, not the publish flow."
        )
    if status == "FAILED":
        raise RuntimeError(f"TikTok publish failed for publish_id={publish_id}")

    return publish_id


def poll_status(publish_id: str, token: str, timeout: int = POLL_TIMEOUT) -> str:
    """Poll /post/publish/status/fetch/ until a terminal status or timeout.
    Returns the last observed status (may be non-terminal on timeout — that's
    logged, not raised, since the draft is already uploaded regardless)."""
    deadline = time.time() + timeout
    status = "PROCESSING_UPLOAD"
    while time.time() < deadline:
        resp = requests.post(
            f"{API_BASE}/post/publish/status/fetch/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"publish_id": publish_id},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json().get("data", {})
        status = body.get("status", status)
        fail_reason = body.get("fail_reason")
        if fail_reason in FATAL_CHECK_FAILURES:
            return fail_reason
        if status in TERMINAL_STATUSES:
            return status
        time.sleep(POLL_INTERVAL)

    logger.warning(
        "TikTok publish_id=%s did not reach a terminal status within %ds (last: %s) — "
        "it's already uploaded and will appear in the inbox regardless.",
        publish_id, timeout, status,
    )
    return status


MAX_SINGLE_CHUNK = 64 * 1024 * 1024   # TikTok's per-chunk ceiling


def _chunk_plan(file_size: int) -> tuple[int, int]:
    """A file at or under TikTok's max single-chunk size (64MB) must be sent as
    exactly ONE chunk — chunk_size=file_size, total_chunk_count=1.

    For larger files, per TikTok's Media Transfer Guide: total_chunk_count is
    video_size // chunk_size (FLOOR, not ceil), and the trailing remainder is
    folded into the LAST chunk rather than becoming its own undersized chunk —
    TikTok allows the final chunk to exceed chunk_size (up to 128MB) but
    rejects one smaller than chunk_size. An earlier even-split (ceil-based)
    attempt still failed live with "invalid_params: The total chunk count is
    invalid" because it used ceil() instead of floor() for total_chunk_count;
    floor + remainder-in-last-chunk is what TikTok's docs actually specify."""
    if file_size <= MAX_SINGLE_CHUNK:
        return file_size, 1
    n_chunks = file_size // CHUNK_SIZE
    return CHUNK_SIZE, n_chunks


def _upload_chunks(
    mp4_path: Path, upload_url: str, file_size: int, chunk_size: int, total_chunk_count: int, token: str
) -> None:
    # total_chunk_count is video_size // chunk_size (floor) — the trailing
    # remainder belongs to the LAST chunk, not a separate undersized one, so
    # the final read pulls in everything left on disk rather than stopping
    # at chunk_size.
    chunk_num = 0
    offset = 0
    with open(mp4_path, "rb") as fh:
        while True:
            is_last = chunk_num == total_chunk_count - 1
            data = fh.read(file_size) if is_last else fh.read(chunk_size)
            if not data:
                break
            end = offset + len(data) - 1
            logger.debug("Uploading chunk %d bytes %d-%d", chunk_num, offset, end)
            resp = requests.put(
                upload_url,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {offset}-{end}/{file_size}",
                    "Content-Length": str(len(data)),
                },
                data=data,
                timeout=120,
            )
            resp.raise_for_status()
            offset += len(data)
            chunk_num += 1
            time.sleep(0.5)   # stay under 6 req/min rate limit


def _refresh_token(cfg, refresh_token: str) -> dict:
    """Exchange a refresh token for a new access token.
    Returns {"access_token": ..., "refresh_token": ..., "expires_in": ...}."""
    resp = requests.post(
        f"{API_BASE}/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": cfg.tiktok_client_key,
            "client_secret": cfg.tiktok_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def token_status(cfg, conn) -> Optional[dict]:
    """For the `status` CLI subcommand: report stored token expiry without refreshing."""
    row = get_token(conn, "tiktok")
    if row is None:
        return {"configured": bool(cfg.tiktok_access_token), "expires_at": None}
    return {"configured": True, "expires_at": row["expires_at"]}


def check_account_health(cfg, conn) -> dict:
    """
    Lightweight account check for the `monitor` CLI subcommand. Needs the
    user.info.basic scope (separate from video.upload, which is all we
    currently request) — add it when doing the TikTok auth flow if this keeps
    reporting the scope error below.
    """
    if not cfg.tiktok_access_token and not cfg.tiktok_refresh_token:
        return {"token_ok": False, "error": "not configured"}

    try:
        token = _ensure_fresh_token(cfg, conn) if conn is not None else cfg.tiktok_access_token
    except CredentialsMissing as e:
        return {"token_ok": False, "error": str(e)}

    # fields must be a JSON-array-encoded query param, not a comma string —
    # a comma string returns a 404 "Unsupported path(Janus)" gateway error
    # that looks like a routing problem but is actually a malformed request.
    resp = requests.get(
        f"{API_BASE}/user/info/",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": '["display_name","follower_count","video_count"]'},
        timeout=15,
    )
    if resp.status_code != 200:
        return {"token_ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}

    body = resp.json()
    err = body.get("error", {})
    if err.get("code") not in (None, "ok"):
        return {"token_ok": False, "error": f"{err.get('code')}: {err.get('message')} (likely needs user.info.basic scope)"}

    data = body.get("data", {}).get("user", {})
    if not data:
        return {"token_ok": True, "followers_count": None, "media_count": None,
                "note": "API call succeeded but returned no profile fields — possibly a Sandbox-mode limitation"}
    return {
        "token_ok": True,
        "followers_count": data.get("follower_count"),
        "media_count": data.get("video_count"),
    }
