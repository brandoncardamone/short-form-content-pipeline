"""
TikTok Content Posting API publisher. Uses FILE_UPLOAD transfer (not PULL_FROM_URL,
which requires domain ownership verification). Videos land in the creator's draft
inbox; a human taps publish.

Scope required: video.upload (not video.publish — that skips the review inbox).
Rate limit: 6 requests/minute per user token.

Gate: if TIKTOK_ACCESS_TOKEN is absent, raise CredentialsMissing.
"""

import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://open.tiktokapis.com/v2"
CHUNK_SIZE = 10 * 1024 * 1024   # 10 MB per chunk (TikTok minimum is 5 MB, max 64 MB)


class CredentialsMissing(Exception):
    pass


def _check_credentials(cfg):
    if not cfg.tiktok_access_token:
        raise CredentialsMissing(
            "TikTok credentials are not configured. "
            "Set TIKTOK_ACCESS_TOKEN in .env."
        )


def upload_draft(mp4_path: Path, caption: str, cfg) -> str:
    """
    Upload a video as a TikTok draft. Returns the publish_id.
    Uses FILE_UPLOAD which does not require a public URL.
    """
    _check_credentials(cfg)
    token = cfg.tiktok_access_token
    file_size = mp4_path.stat().st_size

    # Step 1 — initialize upload
    logger.info("Initializing TikTok upload for %s (%d bytes) …", mp4_path.name, file_size)
    init_resp = requests.post(
        f"{API_BASE}/post/publish/inbox/video/init/",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "post_info": {
                "title": caption[:150],   # TikTok title max 150 chars
                "privacy_level": "SELF_ONLY",   # draft — human publishes
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
                "video_cover_timestamp_ms": 1000,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": CHUNK_SIZE,
                "total_chunk_count": _chunk_count(file_size),
            },
        },
        timeout=30,
    )
    init_resp.raise_for_status()
    data = init_resp.json().get("data", {})
    publish_id = data["publish_id"]
    upload_url = data["upload_url"]
    logger.info("Upload initialized: publish_id=%s", publish_id)

    # Step 2 — upload chunks
    _upload_chunks(mp4_path, upload_url, file_size, token)

    logger.info("TikTok draft submitted: publish_id=%s", publish_id)
    return publish_id


def _chunk_count(file_size: int) -> int:
    return max(1, (file_size + CHUNK_SIZE - 1) // CHUNK_SIZE)


def _upload_chunks(mp4_path: Path, upload_url: str, file_size: int, token: str) -> None:
    chunk_num = 0
    offset = 0
    with open(mp4_path, "rb") as fh:
        while True:
            data = fh.read(CHUNK_SIZE)
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


def refresh_token(cfg) -> dict:
    """
    Exchange a refresh token for a new access token.
    Returns {"access_token": ..., "refresh_token": ..., "expires_in": ...}.
    """
    if not cfg.tiktok_client_key or not cfg.tiktok_client_secret or not cfg.tiktok_refresh_token:
        raise CredentialsMissing(
            "TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET, and TIKTOK_REFRESH_TOKEN must all be set."
        )
    resp = requests.post(
        f"{API_BASE}/oauth/token/",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": cfg.tiktok_client_key,
            "client_secret": cfg.tiktok_client_secret,
            "grant_type": "refresh_token",
            "refresh_token": cfg.tiktok_refresh_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()
