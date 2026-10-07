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

DEFAULT_PROFILE = "main"


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
    # Delivery is randomised once PER VIDEO (never per beat - that would make
    # the narrator's voice change mid-video). Every video before 2026-09-29 was
    # read at exactly the same speed and expressiveness, which is part of why
    # they all sounded like the same video.
    speed_jitter: float = 0.12          # speed becomes speed +/- this
    exaggeration: float = 0.7           # Chatterbox: how animated the read is
    exaggeration_jitter: float = 0.18
    cfg_weight: float = 0.45            # Chatterbox: reference adherence vs variation
    cfg_weight_jitter: float = 0.12


class LLMConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-2.0-flash-exp"
    # The free tier's daily request cap is PER MODEL
    # (GenerateRequestsPerDayPerProjectPerModel-FreeTier), so a second model is
    # a second allowance rather than a shared one. Exhausting one and moving to
    # the next is what keeps the non-Reddit formats actually reaching
    # production - see the note in config.yaml.
    fallback_models: list[str] = []


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
    # Whether a video may mirror its background (see _pick_background_look).
    # Off for footage with on-screen text: the first Terraria build came out
    # with the boss health readout written backwards.
    allow_flip: bool = True


class ExplainerConfig(BaseModel):
    """The recurring duo for the explainer format. They are the channel's
    identity - a reason to follow rather than just watch - so they are config
    rather than constants, and should not be changed casually once an account
    has built any recognition."""
    expert_name: str = "DOC"
    expert_color: str = "#5EC8FF"
    student_name: str = "KIP"
    student_color: str = "#FFE300"


class ContentWeights(BaseModel):
    """Relative odds of each format when content.format is "random". A format
    set to 0 is never produced, which is the way to retire one without
    deleting its generator."""
    reddit_story: int = 3
    textchain: int = 3
    groupchat: int = 2
    wiki_facts: int = 2
    monologue: int = 2
    explainer: int = 0      # account two's format; off by default on the main profile
    terraria: int = 0       # the Terraria account's format; see config.terraria.yaml


class ContentConfig(BaseModel):
    format: str = "textchain"   # textchain | reddit_story | random (cmd_generate picks one per video)
    weights: ContentWeights = ContentWeights()


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
    # IANA name, not "local time" — this now runs on machines in different
    # system timezones (a US-Eastern PC and a UTC GitHub Actions runner), so
    # window_start_hour/window_end_hour must be pinned to one explicit zone
    # rather than each machine's own idea of "now". Fixed 2026-09-09 after
    # confirming live that most randomized slots were silently landing
    # outside the intended 9am-11pm Eastern window because the GitHub Actions
    # runner was computing that window in UTC instead.
    timezone: str = "America/New_York"
    window_start_hour: int = 9    # in `timezone`; slots are randomized within [start, end)
    window_end_hour: int = 23
    platform: str = "both"        # both | instagram | tiktok — passed to the publish step
    min_gap_minutes: int = 30     # don't let two random slots land closer together than this
    # cloud-tick waits a random 0..jitter_max_minutes before publishing, so
    # posts do not land at the same clock times every day even though the cron
    # that triggers them is fixed. Most of this is absorbed by the build, which
    # takes ~25 min anyway - only the remainder is actually slept.
    jitter_max_minutes: int = 45


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
    explainer: ExplainerConfig = ExplainerConfig()

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


def profile_name() -> str:
    """Which account this process is running as.

    One codebase, several accounts. A profile selects a config file, its own
    database (via output.db_path in that file) and its own credentials, so a
    second account is not a second checkout to keep in sync. Set with
    --profile on the CLI or the PROFILE env var; the default profile is the
    original account and behaves exactly as before.
    """
    return os.getenv("PROFILE", "").strip() or DEFAULT_PROFILE


def config_path_for(profile: str) -> Path:
    """config.yaml for the default profile, config.<profile>.yaml otherwise."""
    if profile == DEFAULT_PROFILE:
        return ROOT / "config.yaml"
    return ROOT / f"config.{profile}.yaml"


def _secret(name: str, profile: str, shared: bool = True) -> Optional[str]:
    """Per-profile secret, with a fallback to the shared one where that is safe.

    Looks for NAME_<PROFILE> first, then NAME. So a second account sets
    INSTAGRAM_ACCESS_TOKEN_EDU while still sharing GEMINI_API_KEY, without
    needing every variable duplicated.

    shared=False disables the fallback, and is used for anything that
    identifies WHICH ACCOUNT gets posted to. Falling back there means a profile
    whose own token is missing or misnamed silently publishes its videos to the
    main account - it returns None instead, and the publishers skip cleanly on
    missing credentials.
    """
    if profile != DEFAULT_PROFILE:
        scoped = os.getenv(f"{name}_{profile.upper()}")
        if scoped or not shared:
            return scoped or None
    return os.getenv(name)


def load_config(config_path: Optional[Path] = None) -> Config:
    global _config
    if _config is not None:
        return _config

    load_dotenv(ROOT / ".env")

    profile = profile_name()
    data: dict = {}
    path = config_path or config_path_for(profile)
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    elif profile != DEFAULT_PROFILE:
        raise FileNotFoundError(
            f"No config for profile {profile!r} at {path}. "
            f"Create it (copy config.yaml and give it its own output.db_path)."
        )

    # Explicit override for a host where the configured OneDrive/Dropbox path
    # in config.yaml doesn't exist (e.g. the GitHub Actions runner — see
    # .github/workflows/pipeline.yml) — set MOBILE_SYNC_DIR="" in the
    # environment to disable it there without touching the shared yaml file.
    # Absent means "use whatever config.yaml says", not "disable".
    if "MOBILE_SYNC_DIR" in os.environ:
        data.setdefault("output", {})["mobile_sync_dir"] = os.environ["MOBILE_SYNC_DIR"] or None

    # Overlay secrets from environment — never from yaml
    data["gemini_api_key"] = _secret("GEMINI_API_KEY", profile)
    data["elevenlabs_api_key"] = _secret("ELEVENLABS_API_KEY", profile)
    # Account-identifying, so never inherited from the main account. The app
    # credentials below them are per developer app, not per account, and one
    # app can serve several accounts.
    data["tiktok_access_token"] = _secret("TIKTOK_ACCESS_TOKEN", profile, shared=False)
    data["tiktok_refresh_token"] = _secret("TIKTOK_REFRESH_TOKEN", profile, shared=False)
    data["tiktok_client_key"] = _secret("TIKTOK_CLIENT_KEY", profile)
    data["tiktok_client_secret"] = _secret("TIKTOK_CLIENT_SECRET", profile)
    data["instagram_access_token"] = _secret("INSTAGRAM_ACCESS_TOKEN", profile, shared=False)
    data["instagram_user_id"] = _secret("INSTAGRAM_USER_ID", profile, shared=False)
    data["instagram_app_id"] = _secret("INSTAGRAM_APP_ID", profile)
    data["instagram_app_secret"] = _secret("INSTAGRAM_APP_SECRET", profile)
    data["reddit_client_id"] = _secret("REDDIT_CLIENT_ID", profile)
    data["reddit_client_secret"] = _secret("REDDIT_CLIENT_SECRET", profile)
    data["reddit_user_agent"] = _secret("REDDIT_USER_AGENT", profile)

    _config = Config.model_validate(data)
    return _config
