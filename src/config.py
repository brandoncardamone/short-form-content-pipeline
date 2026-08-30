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


class Config(BaseModel):
    video: VideoConfig = VideoConfig()
    tts: TTSConfig = TTSConfig()
    llm: LLMConfig = LLMConfig()
    card: CardConfig = CardConfig()
    output: OutputConfig = OutputConfig()
    music: MusicConfig = MusicConfig()
    backgrounds: BackgroundsConfig = BackgroundsConfig()

    # Secrets — never in config.yaml, always from environment
    gemini_api_key: Optional[str] = None
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

    # Overlay secrets from environment — never from yaml
    data["gemini_api_key"] = os.getenv("GEMINI_API_KEY")
    data["tiktok_access_token"] = os.getenv("TIKTOK_ACCESS_TOKEN")
    data["tiktok_refresh_token"] = os.getenv("TIKTOK_REFRESH_TOKEN")
    data["tiktok_client_key"] = os.getenv("TIKTOK_CLIENT_KEY")
    data["tiktok_client_secret"] = os.getenv("TIKTOK_CLIENT_SECRET")
    data["instagram_access_token"] = os.getenv("INSTAGRAM_ACCESS_TOKEN")
    data["instagram_user_id"] = os.getenv("INSTAGRAM_USER_ID")
    data["instagram_app_id"] = os.getenv("INSTAGRAM_APP_ID")
    data["instagram_app_secret"] = os.getenv("INSTAGRAM_APP_SECRET")

    _config = Config.model_validate(data)
    return _config
