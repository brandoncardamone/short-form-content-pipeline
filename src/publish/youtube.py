"""
YouTube Shorts publisher via the YouTube Data API v3 resumable upload.

There is no separate Shorts endpoint: a vertical video of three minutes or less
uploaded through videos.insert is classed as a Short by YouTube itself.

Step 1: POST /upload/youtube/v3/videos?uploadType=resumable with the metadata
         → session URL in the Location header
Step 2: PUT the file bytes to the session URL → video resource (id, status)
         A dropped connection is resumed by asking the session how much it
         holds (PUT with "Content-Range: bytes */<size>") and sending the rest.

Token model: an OAuth refresh token for the channel, obtained once with
scripts/youtube_auth.py, plus the Google Cloud project's OAuth client id and
secret. Each publish exchanges the refresh token for a one-hour access token.
That access token is deliberately NOT stored in the `tokens` table: the state
database is committed to a public repo, and there is nothing to gain from
caching a token that is cheap to mint and only needed once per run.

The refresh token stops working if the channel owner revokes access, changes
password, leaves it unused for six months, or if the Cloud project's OAuth
consent screen is left in "Testing" (tokens then die after seven days). Any of
those surfaces as TokenExpired; the fix is re-running scripts/youtube_auth.py.

Gate: if the refresh token or client credentials are absent, raise
CredentialsMissing so the caller can skip cleanly.
"""

import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    # Read-only: lets `monitor` see subscriber and per-video view counts.
    "https://www.googleapis.com/auth/youtube.readonly",
]
UPLOAD_ATTEMPTS = 4
SESSION_ATTEMPTS = 3


class CredentialsMissing(Exception):
    pass


class TokenExpired(Exception):
    """The refresh token was rejected (invalid_grant). Not retryable - re-run
    scripts/youtube_auth.py and update YOUTUBE_REFRESH_TOKEN[_<PROFILE>]."""
    pass


def _check_credentials(cfg):
    if not (cfg.youtube_refresh_token and cfg.youtube_client_id and cfg.youtube_client_secret):
        raise CredentialsMissing(
            "YouTube credentials are not configured. Set YOUTUBE_CLIENT_ID, "
            "YOUTUBE_CLIENT_SECRET and YOUTUBE_REFRESH_TOKEN (see scripts/youtube_auth.py)."
        )


def access_token(cfg) -> str:
    _check_credentials(cfg)
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": cfg.youtube_client_id,
            "client_secret": cfg.youtube_client_secret,
            "refresh_token": cfg.youtube_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if resp.status_code == 400 and "invalid_grant" in resp.text:
        raise TokenExpired(
            "YouTube refresh token was rejected (invalid_grant) - re-run "
            "scripts/youtube_auth.py. If this happens every 7 days, the Cloud "
            "project's OAuth consent screen is still in Testing; publish it."
        )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _error_text(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error", {})
        reasons = ",".join(e.get("reason", "") for e in err.get("errors", []))
        return f"{resp.status_code} {err.get('message', '')} [{reasons}]"
    except ValueError:
        return f"{resp.status_code} {resp.text[:300]}"


def publish_short(mp4_path: Path, title: str, description: str, tags: list[str], cfg) -> str:
    """Upload a Short. Returns the YouTube video id.
    Raises CredentialsMissing if credentials are absent."""
    token = access_token(cfg)
    yc = cfg.youtube
    metadata = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "categoryId": yc.category_id,
        },
        "status": {
            "privacyStatus": yc.privacy,
            "selfDeclaredMadeForKids": False,
            # YouTube's "altered or synthetic content" disclosure.
            "containsSyntheticMedia": yc.synthetic_media,
        },
    }
    file_size = mp4_path.stat().st_size

    for attempt in range(1, SESSION_ATTEMPTS + 1):
        session = _start_session(metadata, file_size, token)
        try:
            video = _send_bytes(session, mp4_path, file_size)
            break
        except requests.RequestException as e:
            logger.warning("YouTube upload session %d/%d failed: %s", attempt, SESSION_ATTEMPTS, e)
            if attempt == SESSION_ATTEMPTS:
                raise
            time.sleep(10 * attempt)

    status = video.get("status", {})
    upload_status = status.get("uploadStatus")
    if upload_status in ("rejected", "failed"):
        reason = status.get("rejectionReason") or status.get("failureReason")
        raise RuntimeError(f"YouTube {upload_status} the upload: {reason}")
    video_id = video["id"]
    logger.info("Uploaded to YouTube: https://youtube.com/shorts/%s (privacy=%s)",
                video_id, status.get("privacyStatus"))
    return video_id


def _start_session(metadata: dict, file_size: int, token: str) -> str:
    resp = requests.post(
        UPLOAD_URL,
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(file_size),
        },
        json=metadata,
        timeout=30,
    )
    if resp.status_code != 200:
        # 4xx here is a real problem (quota, bad metadata, channel not allowed
        # to upload) - surface YouTube's own reason rather than a bare status.
        raise RuntimeError(f"YouTube upload session refused: {_error_text(resp)}")
    return resp.headers["Location"]


def _send_bytes(session: str, mp4_path: Path, file_size: int) -> dict:
    """PUT the file, resuming from whatever the session already holds after a
    dropped connection or a 5xx."""
    with open(mp4_path, "rb") as fh:
        body = fh.read()

    offset = 0
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        headers = {"Content-Type": "video/mp4"}
        if offset:
            headers["Content-Range"] = f"bytes {offset}-{file_size - 1}/{file_size}"
        try:
            resp = requests.put(session, headers=headers, data=body[offset:], timeout=600)
        except requests.RequestException as e:
            logger.warning("YouTube upload attempt %d/%d dropped: %s", attempt, UPLOAD_ATTEMPTS, e)
            resp = None

        if resp is not None and resp.status_code in (200, 201):
            return resp.json()
        if resp is not None and resp.status_code < 500 and resp.status_code != 308:
            raise RuntimeError(f"YouTube upload rejected: {_error_text(resp)}")
        if attempt == UPLOAD_ATTEMPTS:
            break
        time.sleep(5 * attempt)
        offset = _committed_offset(session, file_size)
        logger.info("Resuming YouTube upload from byte %d of %d", offset, file_size)

    raise requests.ConnectionError(f"YouTube upload did not complete after {UPLOAD_ATTEMPTS} attempts")


def _committed_offset(session: str, file_size: int) -> int:
    """How many bytes the session holds. 308 + "Range: bytes=0-N" means N+1;
    308 with no Range means none."""
    resp = requests.put(session, headers={"Content-Range": f"bytes */{file_size}"}, timeout=30)
    if resp.status_code == 308:
        rng = resp.headers.get("Range")
        return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
    if resp.status_code in (200, 201):
        return file_size
    resp.raise_for_status()
    return 0


def token_status(cfg, conn=None) -> dict:
    """For the `status` CLI subcommand."""
    configured = bool(cfg.youtube_refresh_token and cfg.youtube_client_id
                      and cfg.youtube_client_secret)
    return {"configured": configured, "expires_at": None}


def check_account_health(cfg, conn=None) -> dict:
    """Subscriber and video counts for the channel the token belongs to."""
    try:
        token = access_token(cfg)
    except CredentialsMissing:
        return {"token_ok": False, "error": "not configured"}
    except (TokenExpired, requests.RequestException) as e:
        return {"token_ok": False, "error": str(e)}

    resp = requests.get(
        f"{API_BASE}/channels",
        params={"part": "snippet,statistics", "mine": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code != 200:
        return {"token_ok": False, "error": _error_text(resp)}
    items = resp.json().get("items") or []
    if not items:
        return {"token_ok": False, "error": "token has no YouTube channel"}
    stats = items[0].get("statistics", {})
    return {
        "token_ok": True,
        "channel": items[0]["snippet"].get("title"),
        "followers_count": int(stats.get("subscriberCount", 0)),
        "media_count": int(stats.get("videoCount", 0)),
    }


def fetch_video_stats(cfg, video_ids: list[str]) -> list[dict]:
    """Per-video views/likes/comments and privacy - the view counts Instagram's
    API will not give this app."""
    if not video_ids:
        return []
    token = access_token(cfg)
    out = []
    for i in range(0, len(video_ids), 50):
        resp = requests.get(
            f"{API_BASE}/videos",
            params={"part": "statistics,status,snippet", "id": ",".join(video_ids[i:i + 50])},
            headers={"Authorization": f"Bearer {token}"},
            timeout=20,
        )
        resp.raise_for_status()
        for v in resp.json().get("items", []):
            s = v.get("statistics", {})
            out.append({
                "youtube_id": v["id"],
                "title": v["snippet"].get("title"),
                "privacy": v["status"].get("privacyStatus"),
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)),
            })
    return out
