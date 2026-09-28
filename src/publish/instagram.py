"""
Instagram Reels publisher via the Graph API's resumable-upload container flow.

This uploads raw local bytes — it does NOT require the file to be publicly
reachable (unlike the `video_url` container path, which is wrong for a local
pipeline since nothing here is served over the internet).

Step 1: POST /{ig-user-id}/media?upload_type=resumable  (host graph.facebook.com)
         → container_id
Step 2: POST https://rupload.facebook.com/ig-api-upload/{container_id}
         with raw bytes, Authorization: OAuth <token>, offset/file_size headers
Step 3: Poll container until status_code == FINISHED (can take minutes)
Step 4: POST /{ig-user-id}/media_publish with creation_id → post_id

Requires the app to have Facebook Login for Business implemented (per Meta's
resumable-upload requirements).

Token model — NOT the same as a typical expiring API key, and there is no
programmatic refresh for it:
  INSTAGRAM_ACCESS_TOKEN here is a **Page access token obtained via a
  long-lived User access token** (the manual `dialog/oauth` consent flow +
  `fb_exchange_token` exchange + `/me/accounts`). Meta documents this specific
  token shape as effectively non-expiring — it only stops working if the
  password changes, the app's permissions are revoked, or similar. There is no
  `ig_refresh_token`-style endpoint for it (that grant type is for a different,
  Instagram-Login-based token flow and returns a GraphMethodException here —
  confirmed directly against the live API, not assumed from docs). So this
  module does NOT attempt proactive refresh: it uses the stored/env token as-is
  and only surfaces a clear error if the API itself ever rejects it, at which
  point the fix is re-running the manual consent flow, not an automated retry.

Gate: if INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_USER_ID are absent, raise
CredentialsMissing so the caller can skip cleanly.
"""

import logging
import time
from pathlib import Path
from typing import Optional

import requests

from src.db import get_token

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v25.0"
RUPLOAD_BASE = "https://rupload.facebook.com/ig-api-upload"
POLL_INTERVAL = 10    # seconds
POLL_TIMEOUT = 600    # seconds — processing can take minutes, per spec
CONTAINER_RETRY_LIMIT = 3  # attempts, each with a fresh container and a full re-upload
AUTH_ERROR_CODES = {190}   # OAuthException — token invalid/revoked


class CredentialsMissing(Exception):
    pass


class TokenExpired(Exception):
    """Raised when the Page access token is rejected by the API. There is no
    automated refresh for this token type — fix is to re-run the manual
    dialog/oauth consent flow and update INSTAGRAM_ACCESS_TOKEN in .env."""
    pass


def _check_credentials(cfg):
    if not cfg.instagram_access_token or not cfg.instagram_user_id:
        raise CredentialsMissing(
            "Instagram credentials are not configured. "
            "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID in .env."
        )


def _current_token(cfg, conn) -> str:
    """Use the DB-stored token if one was ever persisted (e.g. from a prior
    manual re-auth), else fall back to the .env value."""
    if conn is not None:
        row = get_token(conn, "instagram")
        if row is not None:
            return row["access_token"]
    return cfg.instagram_access_token


def _raise_if_auth_error(resp: requests.Response) -> None:
    if resp.status_code == 400:
        try:
            code = resp.json().get("error", {}).get("code")
        except ValueError:
            code = None
        if code in AUTH_ERROR_CODES:
            raise TokenExpired(
                "Instagram access token was rejected. This token type has no automated "
                "refresh — re-run the manual dialog/oauth consent flow (see project memory) "
                "and update INSTAGRAM_ACCESS_TOKEN in .env."
            )


def publish_reel(mp4_path: Path, caption: str, cfg, conn=None, cover_ms: int = 0) -> str:
    """
    Publish a Reel from a local file. Returns the Instagram post ID.
    Raises CredentialsMissing if credentials are absent.

    cover_ms: millisecond offset into the video for the feed thumbnail
    (`thumb_offset`). Matters for click-through — a mid-animation frame looks
    broken as a still cover.
    """
    _check_credentials(cfg)
    token = _current_token(cfg, conn)
    user_id = cfg.instagram_user_id
    file_size = mp4_path.stat().st_size

    # Steps 1-3 (create container → upload bytes → wait for processing) retry as
    # a unit against a FRESH container each time. A container that has rejected
    # its bytes once is not reusable: every later upload into it returns "The ig
    # container is not in the status to upload a video" or a bare "Request
    # processing failed". So _upload_bytes' in-container retries can only
    # recover a transfer interrupted mid-flight — never a rejection. Confirmed
    # live 2026-09-27, when all three in-container attempts failed identically
    # after the real cause (a transcoder rejection, since fixed in
    # assemble/build.py) poisoned the container on the first attempt.
    for attempt in range(1, CONTAINER_RETRY_LIMIT + 1):
        try:
            container_id = _stage_container(
                user_id, mp4_path, file_size, caption, cover_ms, token
            )
            break
        except TokenExpired:
            raise   # no amount of retrying fixes a rejected token
        except (requests.RequestException, RuntimeError) as e:
            logger.warning("Staging attempt %d/%d failed: %s",
                           attempt, CONTAINER_RETRY_LIMIT, e)
            if attempt == CONTAINER_RETRY_LIMIT:
                raise
            time.sleep(10 * attempt)

    # Step 4 — publish
    logger.info("Publishing container %s …", container_id)
    resp = requests.post(
        f"{GRAPH_BASE}/{user_id}/media_publish",
        params={"creation_id": container_id, "access_token": token},
        timeout=30,
    )
    resp.raise_for_status()
    post_id = resp.json()["id"]
    logger.info("Published: %s", post_id)
    return post_id


def _stage_container(
    user_id: str,
    mp4_path: Path,
    file_size: int,
    caption: str,
    cover_ms: int,
    token: str,
) -> str:
    """Create a container, push the bytes into it, and wait for Instagram to
    finish processing them. Returns a container ready to publish. Any failure
    leaves the container unusable, so the caller retries with a new one."""
    logger.info("Creating IG Reels resumable container …")
    resp = requests.post(
        f"{GRAPH_BASE}/{user_id}/media",
        params={
            "upload_type": "resumable",
            "media_type": "REELS",
            "caption": caption,
            "thumb_offset": cover_ms,
            "access_token": token,
        },
        timeout=30,
    )
    _raise_if_auth_error(resp)
    resp.raise_for_status()
    container_id = resp.json()["id"]
    logger.info("Container created: %s", container_id)

    # A freshly-created container's upload endpoint isn't always immediately
    # ready — early publishes saw the first full-file upload fail with a 400
    # after transferring the whole file, then succeed on a second try. This
    # cheap status probe appears to settle the container without wasting a full
    # failed transfer first. Kept for that reason only; its returned offset is
    # deliberately ignored, since resuming from it is what corrupted uploads.
    _query_uploaded_offset(container_id, token, 0)

    _upload_bytes(container_id, mp4_path, file_size, token)
    _wait_for_container(container_id, token)
    return container_id


def _upload_bytes(container_id: str, mp4_path: Path, file_size: int, token: str) -> None:
    """Upload the whole file in one resumable request, always from offset 0.

    This deliberately does NOT resume from a partial offset, and that is the
    point. It used to: on failure it asked rupload how many bytes it had
    ("offset" response header) and re-sent only the remainder. The failure
    signature that produced says the resumed body is not being reassembled into
    a valid file — across every observed failure, attempt 1 (full file, offset
    0) came back a generic 400 "Request processing failed", and attempt 2 (the
    resumed one) came back 500 "Video Transcoding Error: both HD and SD
    progressive failed to transcode". The transcode error appeared ONLY on
    resumed attempts, never on a first full upload, and a first full upload that
    succeeds transcodes fine. A partial resume that the server then treats as a
    complete file is exactly what would produce an undecodable video.

    A full re-upload costs seconds at these file sizes (~25-40MB), so resuming
    was never buying much. publish_reel retries against a FRESH container, which
    is the only clean way to retry anyway: a container that has rejected its
    bytes once will not accept more into the same id.

    offset/file_size headers are still required — Meta's error for omitting them
    is a confusing ParameterValidationError about the Offset header.
    """
    with open(mp4_path, "rb") as fh:
        body = fh.read()

    resp = requests.post(
        f"{RUPLOAD_BASE}/{container_id}",
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Type": "application/octet-stream",
        },
        data=body,
        timeout=180,
    )
    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        # requests' str() on an HTTPError is just status+URL; Meta's actual
        # rejection reason is in the body, and without it a recurring failure is
        # undiagnosable from logs alone.
        body_text = e.response.text[:500] if e.response is not None else None
        logger.warning("Upload to container %s failed: %s | response body: %s",
                       container_id, e, body_text)
        raise
    logger.info("Uploaded %d bytes to container %s", file_size, container_id)


def _query_uploaded_offset(container_id: str, token: str, fallback: int) -> int:
    """Best-effort probe used to settle a freshly-created container before the
    upload. The returned offset is NOT used to resume a transfer — see
    _upload_bytes for why resuming corrupted the uploaded file."""
    try:
        resp = requests.get(
            f"{RUPLOAD_BASE}/{container_id}",
            headers={"Authorization": f"OAuth {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        return int(resp.headers.get("offset", fallback))
    except requests.RequestException:
        return fallback


def _wait_for_container(container_id: str, token: str) -> str:
    deadline = time.time() + POLL_TIMEOUT
    delay = 2
    while time.time() < deadline:
        resp = requests.get(
            f"{GRAPH_BASE}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=15,
        )
        resp.raise_for_status()
        status = resp.json().get("status_code")
        logger.debug("Container %s status: %s", container_id, status)

        if status == "FINISHED":
            return container_id
        if status == "ERROR":
            raise RuntimeError(f"Instagram container {container_id} errored during processing")
        if status == "EXPIRED":
            raise RuntimeError(f"Instagram container {container_id} expired before publishing")

        time.sleep(delay)
        delay = min(delay * 2, 30)   # 2s, 4s, 8s, 16s, cap 30s

    raise TimeoutError(
        f"Instagram container {container_id} did not finish within {POLL_TIMEOUT}s"
    )


def token_status(cfg, conn) -> Optional[dict]:
    """For the `status` CLI subcommand. This token type has no tracked expiry
    (see module docstring) — reports whether one is configured, not a countdown."""
    return {"configured": bool(_current_token(cfg, conn)), "expires_at": None}


def check_account_health(cfg, conn) -> dict:
    """
    Lightweight account check for the `monitor` CLI subcommand — does NOT use
    the /insights endpoint (requires instagram_manage_insights, which this app
    doesn't have, and Reels insights are gated behind 1,000 followers regardless
    of permissions). Uses only followers_count/media_count, which work with the
    basic permissions already granted, so this is available from day one.
    """
    if not cfg.instagram_access_token or not cfg.instagram_user_id:
        return {"token_ok": False, "error": "not configured"}

    token = _current_token(cfg, conn)
    resp = requests.get(
        f"{GRAPH_BASE}/{cfg.instagram_user_id}",
        params={"fields": "followers_count,media_count", "access_token": token},
        timeout=15,
    )
    if resp.status_code != 200:
        try:
            err = resp.json().get("error", {}).get("message", resp.text[:200])
        except ValueError:
            err = resp.text[:200]
        return {"token_ok": False, "error": err}

    data = resp.json()
    return {
        "token_ok": True,
        "followers_count": data.get("followers_count"),
        "media_count": data.get("media_count"),
    }
