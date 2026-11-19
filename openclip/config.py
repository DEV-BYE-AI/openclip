from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

HOME_DIR = Path(os.environ.get("OPENCLIP_HOME", Path.home() / ".openclip"))
TOKENS_DIR = HOME_DIR / "tokens"


@dataclass
class ClipSettings:
    min_seconds: float = 30.0
    max_seconds: float = 60.0
    num_clips: int = 5
    crop_mode: str = "face"  # face | center | blur
    subtitles: bool = True
    language: str | None = None  # None = détection auto
    width: int = 1080
    height: int = 1920


@dataclass
class Settings:
    anthropic_model: str = os.environ.get("OPENCLIP_MODEL", "claude-opus-5-5")
    whisper_model: str = os.environ.get("WHISPER_MODEL", "small")
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "cpu")
    tiktok_client_key: str = os.environ.get("TIKTOK_CLIENT_KEY", "")
    tiktok_client_secret: str = os.environ.get("TIKTOK_CLIENT_SECRET", "")
    tiktok_redirect_uri: str = os.environ.get(
        "TIKTOK_REDIRECT_URI", "http://localhost:8765/callback"
    )
    youtube_client_secrets: str = os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secret.json")
    clip: ClipSettings = field(default_factory=ClipSettings)


def ensure_dirs() -> None:
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
