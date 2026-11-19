from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# .env du dossier courant d'abord, sinon celui à la racine du projet
load_dotenv(find_dotenv(usecwd=True) or Path(__file__).resolve().parent.parent / ".env")

HOME_DIR = Path(os.environ.get("OPENCLIP_HOME", Path.home() / ".openclip"))
TOKENS_DIR = HOME_DIR / "tokens"
# identifiants d'applications saisis depuis l'interface (prioritaires sur .env)
CREDENTIALS_FILE = HOME_DIR / "credentials.json"
YOUTUBE_CLIENT_FILE = HOME_DIR / "youtube_client_secret.json"


@dataclass
class ClipSettings:
    min_seconds: float = 30.0
    max_seconds: float = 60.0
    num_clips: int = 5
    crop_mode: str = "face"  # face | center | blur
    subtitles: bool = True
    caption_style: str = "pop"  # voir subtitles.STYLES
    jump_cuts: bool = True  # supprime les blancs > 0,6 s
    progress_bar: bool = True
    language: str | None = None  # None = détection auto
    width: int = 1080
    height: int = 1920


@dataclass
class Settings:
    anthropic_model: str = os.environ.get("OPENCLIP_MODEL", "claude-opus-5-5")
    whisper_model: str = os.environ.get("WHISPER_MODEL", "small")
    whisper_device: str = os.environ.get("WHISPER_DEVICE", "cpu")
    # utiliser les sous-titres automatiques de YouTube quand ils existent (bien plus rapide)
    youtube_captions: bool = os.environ.get("YOUTUBE_CAPTIONS", "1") != "0"
    tiktok_client_key: str = os.environ.get("TIKTOK_CLIENT_KEY", "")
    tiktok_client_secret: str = os.environ.get("TIKTOK_CLIENT_SECRET", "")
    tiktok_redirect_uri: str = os.environ.get(
        "TIKTOK_REDIRECT_URI", "http://localhost:8765/callback"
    )
    youtube_client_secrets: str = os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secret.json")
    tiktok_mode: str = os.environ.get("TIKTOK_MODE", "draft")  # draft (brouillon) ou direct
    clip: ClipSettings = field(default_factory=ClipSettings)

    def __post_init__(self) -> None:
        self.reload_credentials()

    def reload_credentials(self) -> None:
        saved = load_credentials()
        for key in ("tiktok_client_key", "tiktok_client_secret", "tiktok_redirect_uri", "tiktok_mode"):
            if saved.get(key):
                setattr(self, key, saved[key])
        if YOUTUBE_CLIENT_FILE.exists():
            self.youtube_client_secrets = str(YOUTUBE_CLIENT_FILE)


def load_credentials() -> dict:
    import json

    try:
        return json.loads(CREDENTIALS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_credentials(**values) -> dict:
    import json

    ensure_dirs()
    data = {**load_credentials(), **{k: v for k, v in values.items() if v is not None}}
    CREDENTIALS_FILE.write_text(json.dumps(data, indent=2))
    CREDENTIALS_FILE.chmod(0o600)
    return data


def ensure_dirs() -> None:
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)


# Préréglages de durée. Les durées tiennent compte des règles de monétisation (fin 2026) :
# - TikTok Creator Rewards ne rémunère que les vidéos de plus d'1 minute ;
# - YouTube Shorts partage les revenus publicitaires sur les Shorts (jusqu'à 3 min).
PRESETS: dict[str, tuple[float, float]] = {
    "shorts": (30, 60),     # format court classique, meilleure rétention
    "rewards": (62, 95),    # éligible TikTok Creator Rewards (> 1 min)
    "long": (90, 170),      # histoires complètes, Shorts jusqu'à 3 min
}
