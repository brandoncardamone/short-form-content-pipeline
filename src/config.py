"""
Config loader. Reads config.yaml, overlays .env / environment variables,
validates with Pydantic. All tunable pipeline values live here.
"""

from pathlib import Path
from typing import Optional
import os

import yaml
from pydantic import BaseModel
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent


class VideoConfig(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    target_duration_min: int = 60
    target_duration_max: int = 90


class VoicesConfig(BaseModel):
    a: str = "af_bella"
    b: str = "am_adam"


class TTSConfig(BaseModel):
    engine: str = "kokoro"
    speed: float = 1.2
    gap_ms: float = 220.0
    voices: VoicesConfig = VoicesConfig()


class LLMConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-2.0-flash-exp"


class CardConfig(BaseModel):
    contact_name: str = "Maddy ❤️"
    avatar: Optional[str] = None


class OutputConfig(BaseModel):
    work_dir: Path = Path("output/work")
    ready_dir: Path = Path("output/ready")
    db_path: Path = Path("data/state.db")
    # Optional: a OneDrive/Dropbox/etc-synced folder. If set, each assembled
    # video's mp4 + a caption.txt are copied here automatically — lets you open
    # the caption on your phone (via the sync app) to paste into TikTok's draft,
    # since TikTok's Sandbox mode doesn't reliably pre-fill it from the API.
    mobile_sync_dir: Optional[Path] = None
    # After each sync, oldest video+caption pairs in mobile_sync_dir are
    # deleted (oldest mtime first) until the folder's total size is back
    # under this cap — keeps continuous backlog generation from filling up
    # the synced drive. Does not touch output/ready_dir, which is what the
    # publish stage actually uploads from — only the OneDrive courtesy copy.
    mobile_sync_cap_gb: float = 1.0


class MusicConfig(BaseModel):
    enabled: bool = False
    volume_db: float = -28.0


class BackgroundClipConfig(BaseModel):
    name: str
    path: Path
    crop_x: Optional[int] = None   # None = center crop


class BackgroundsConfig(BaseModel):
    normalized_dir: Path = Path("assets/backgrounds/normalized")
    clips: list[BackgroundClipConfig] = []


class ContentConfig(BaseModel):
    format: str = "textchain"   # textchain | reddit_story | random (cmd_generate picks one per video)


class TikTokConfig(BaseModel):
    # direct: video.publish scope, caption/hashtags apply automatically, posts
    #   live immediately at whatever privacy_level TikTok allows (SELF_ONLY
    #   while unaudited — flip to "Everyone" manually per video afterward).
    # inbox: video.upload scope, human reviews/finishes posting in the TikTok
    #   app, but the API-sent caption is structurally ignored by that flow
    #   (TikTok's own design — not fixable in code, confirmed 2026-09-02).
    post_mode: str = "direct"


class ScheduleConfig(BaseModel):
    posts_per_day: int = 3
    window_start_hour: int = 9    # local time; slots are randomized within [start, end)
    window_end_hour: int = 23
    platform: str = "both"        # both | instagram | tiktok — passed to the publish step
    min_gap_minutes: int = 30     # don't let two random slots land closer together than this


class RedditConfig(BaseModel):
    access: str = "arctic"       # arctic (no auth, archive w/ settled scores) | json | praw (registered app)
    subreddits: list[str] = ["AskReddit", "tifu", "AmItheAsshole", "relationship_advice", "confession"]
    mode: str = "auto"           # auto | narrative | qa — auto picks by whether selftext is present
    listing: str = "top"         # top | hot — json/praw only
    time_filter: str = "month"   # for listing=top: day|week|month|year|all — json/praw only
    min_score: int = 2000
    max_comments: int = 15
    min_comments: int = 5        # reject a qa-mode post if fewer than this many comments qualify
    min_comment_score: int = 20
    allow_nsfw: bool = False
    fetch_pool_size: int = 25    # posts to consider per generate call before filtering/dedup
    archive_min_age_days: int = 180   # arctic only: skip posts newer than this (scores not settled yet)
    archive_max_age_days: int = 730   # arctic only: skip posts older than this
    archive_samples: int = 10         # arctic only: number of random time-slices to sample per
                                       # subreddit — a single contiguous slice badly undersamples
                                       # high-volume subs like AskReddit (thousands of posts/day),
                                       # so spread queries across the whole age window instead
    archive_sample_span_days: int = 3 # arctic only: width of each sampled time-slice, in days


class Config(BaseModel):
    video: VideoConfig = VideoConfig()
    tts: TTSConfig = TTSConfig()
    llm: LLMConfig = LLMConfig()
    card: CardConfig = CardConfig()
    output: OutputConfig = OutputConfig()
    music: MusicConfig = MusicConfig()
    backgrounds: BackgroundsConfig = BackgroundsConfig()
    content: ContentConfig = ContentConfig()
    reddit: RedditConfig = RedditConfig()
    tiktok: TikTokConfig = TikTokConfig()
    schedule: ScheduleConfig = ScheduleConfig()

    # Secrets — never in config.yaml, always from environment
    gemini_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    reddit_client_id: Optional[str] = None
    reddit_client_secret: Optional[str] = None
    reddit_user_agent: Optional[str] = None
    tiktok_access_token: Optional[str] = None
    tiktok_refresh_token: Optional[str] = None
    tiktok_client_key: Optional[str] = None
    tiktok_client_secret: Optional[str] = None
    instagram_access_token: Optional[str] = None
    instagram_user_id: Optional[str] = None
    instagram_app_id: Optional[str] = None
    instagram_app_secret: Optional[str] = None


_config: Optional[Config] = None


def load_config(config_path: Optional[Path] = None) -> Config:
    global _config
    if _config is not None:
        return _config

    load_dotenv(ROOT / ".env")

    data: dict = {}
    path = config_path or ROOT / "config.yaml"
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}

    # Explicit override for a host where the configured OneDrive/Dropbox path
    # in config.yaml doesn't exist (e.g. the GitHub Actions runner — see
    # .github/workflows/pipeline.yml) — set MOBILE_SYNC_DIR="" in the
    # environment to disable it there without touching the shared yaml file.
    # Absent means "use whatever config.yaml says", not "disable".
    if "MOBILE_SYNC_DIR" in os.environ:
        data.setdefault("output", {})["mobile_sync_dir"] = os.environ["MOBILE_SYNC_DIR"] or None

    # Overlay secrets from environment — never from yaml
    data["gemini_api_key"] = os.getenv("GEMINI_API_KEY")
    data["elevenlabs_api_key"] = os.getenv("ELEVENLABS_API_KEY")
    data["tiktok_access_token"] = os.getenv("TIKTOK_ACCESS_TOKEN")
    data["tiktok_refresh_token"] = os.getenv("TIKTOK_REFRESH_TOKEN")
    data["tiktok_client_key"] = os.getenv("TIKTOK_CLIENT_KEY")
    data["tiktok_client_secret"] = os.getenv("TIKTOK_CLIENT_SECRET")
    data["instagram_access_token"] = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    data["instagram_user_id"] = os.getenv("INSTAGRAM_USER_ID")
    data["instagram_app_id"] = os.getenv("INSTAGRAM_APP_ID")
    data["instagram_app_secret"] = os.getenv("INSTAGRAM_APP_SECRET")
    data["reddit_client_id"] = os.getenv("REDDIT_CLIENT_ID")
    data["reddit_client_secret"] = os.getenv("REDDIT_CLIENT_SECRET")
    data["reddit_user_agent"] = os.getenv("REDDIT_USER_AGENT")

    _config = Config.model_validate(data)
    return _config
