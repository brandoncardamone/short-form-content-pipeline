"""
Instagram Reels publisher via Graph API two-step container publish.

Step 1: POST /{ig-user-id}/media  → container_id
Step 2: Poll container until status_code == FINISHED (can take minutes)
Step 3: POST /{ig-user-id}/media_publish with creation_id → post_id

NOTE on video_url: the Graph API requires a publicly reachable URL; it does NOT
support local file uploads. For a fully local pipeline the options are:
  a) Upload the file to a temporary public host (e.g. a free Cloudflare R2 bucket
     with a presigned URL, or any static host you control).
  b) Run a short-lived ngrok tunnel to serve the local file.
The caller is responsible for providing a reachable video_url. This module only
handles the API interaction, not file hosting.

Gate: if INSTAGRAM_ACCESS_TOKEN or INSTAGRAM_USER_ID are absent, raise
CredentialsMissing so the caller can skip cleanly.
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.instagram.com/v21.0"
POLL_INTERVAL = 10    # seconds
POLL_TIMEOUT = 300    # seconds before giving up


class CredentialsMissing(Exception):
    pass


def _check_credentials(cfg):
    if not cfg.instagram_access_token or not cfg.instagram_user_id:
        raise CredentialsMissing(
            "Instagram credentials are not configured. "
            "Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_USER_ID in .env."
        )


def publish_reel(
    video_url: str,
    caption: str,
    cfg,
) -> str:
    """
    Publish a Reel. Returns the Instagram post ID.
    Raises CredentialsMissing if credentials are absent.
    """
    _check_credentials(cfg)
    token = cfg.instagram_access_token
    user_id = cfg.instagram_user_id

    # Step 1 — create container
    logger.info("Creating IG Reels container …")
    resp = requests.post(
        f"{GRAPH_BASE}/{user_id}/media",
        params={
            "media_type": "REELS",
            "video_url": video_url,
            "caption": caption,
            "access_token": token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    container_id = resp.json()["id"]
    logger.info("Container created: %s", container_id)

    # Step 2 — poll until FINISHED
    container_id = _wait_for_container(container_id, token)

    # Step 3 — publish
    logger.info("Publishing container %s …", container_id)
    resp = requests.post(
        f"{GRAPH_BASE}/{user_id}/media_publish",
        params={
            "creation_id": container_id,
            "access_token": token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    post_id = resp.json()["id"]
    logger.info("Published: %s", post_id)
    return post_id


def _wait_for_container(container_id: str, token: str) -> str:
    deadline = time.time() + POLL_TIMEOUT
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

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"Instagram container {container_id} did not finish within {POLL_TIMEOUT}s"
    )


def refresh_token(cfg) -> str:
    """Refresh a long-lived Instagram access token (valid for 60 days; call monthly)."""
    _check_credentials(cfg)
    resp = requests.get(
        f"{GRAPH_BASE}/refresh_access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": cfg.instagram_access_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]
