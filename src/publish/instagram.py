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
UPLOAD_RETRY_LIMIT = 3
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

    # Step 1 — create a resumable-upload container
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
    # ready — every publish so far has seen the first full-file upload attempt
    # fail with a 400 after transferring the whole file, succeeding instantly
    # on retry. A cheap status probe here (the same GET the retry path already
    # uses) seems to be what makes the container ready, without wasting a full
    # failed transfer first. Experimental — remove this comment once confirmed
    # across a few more publishes either way.
    _query_uploaded_offset(container_id, token, 0)

    # Step 2 — upload raw bytes
    _upload_bytes(container_id, mp4_path, file_size, token)

    # Step 3 — poll until FINISHED
    _wait_for_container(container_id, token)

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


def _upload_bytes(container_id: str, mp4_path: Path, file_size: int, token: str) -> None:
    """Upload the whole file in one resumable request, retrying from the last
    confirmed offset on failure (offset/file_size headers are required — Meta's
    error for omitting them is a confusing ParameterValidationError about Offset)."""
    offset = 0
    for attempt in range(1, UPLOAD_RETRY_LIMIT + 1):
        try:
            with open(mp4_path, "rb") as fh:
                fh.seek(offset)
                body = fh.read()
            resp = requests.post(
                f"{RUPLOAD_BASE}/{container_id}",
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": str(offset),
                    "file_size": str(file_size),
                    "Content-Type": "application/octet-stream",
                },
                data=body,
                timeout=180,
            )
            resp.raise_for_status()
            logger.info("Uploaded %d bytes to container %s", file_size - offset, container_id)
            return
        except requests.RequestException as e:
            logger.warning("Upload attempt %d/%d failed: %s", attempt, UPLOAD_RETRY_LIMIT, e)
            offset = _query_uploaded_offset(container_id, token, offset)
            if attempt == UPLOAD_RETRY_LIMIT:
                raise


def _query_uploaded_offset(container_id: str, token: str, fallback: int) -> int:
    """Best-effort: ask how many bytes were received so far, to resume from there.
    Falls back to the last known offset if the query itself fails."""
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
