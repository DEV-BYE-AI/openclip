"""Téléchargement des vidéos YouTube avec yt-dlp."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import os

import yt_dlp


@dataclass
class SourceVideo:
    video_id: str
    title: str
    path: Path
    duration: float
    url: str
    uploader: str = ""


def download(url: str, out_dir: Path) -> SourceVideo:
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        # 1080p max, mp4 de préférence (plus rapide à ré-encoder)
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    # Sur un serveur, YouTube demande souvent « confirme que tu n'es pas un robot » :
    # exporte les cookies de ton navigateur (format Netscape) et indique le fichier ici.
    # YouTube exige maintenant un moteur JavaScript (deno) pour certaines vidéos
    import shutil
    import sys
    deno = shutil.which("deno") or str(Path(sys.executable).with_name("deno"))
    if Path(deno).exists():
        opts["js_runtimes"] = {"deno": {"path": deno}}
    cookies = cookies_path()
    if cookies:
        opts["cookiefile"] = str(cookies)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
        if not path.exists():  # extension différente selon le format retenu
            candidates = sorted(out_dir.glob(f"{info['id']}.*"))
            path = next(p for p in candidates if p.suffix not in {".part", ".ytdl"})
    return SourceVideo(
        video_id=info["id"],
        title=info.get("title", info["id"]),
        path=path,
        duration=float(info.get("duration") or 0),
        url=info.get("webpage_url", url),
        uploader=info.get("uploader", ""),
    )


class BotCheckError(RuntimeError):
    """YouTube demande de prouver qu'on n'est pas un robot (fréquent depuis un serveur)."""


def cookies_path() -> Path | None:
    """Fichier cookies.txt : YTDLP_COOKIES, sinon celui envoyé depuis l'interface."""
    from .config import HOME_DIR

    env = os.environ.get("YTDLP_COOKIES")
    if env and Path(env).exists():
        return Path(env)
    default = HOME_DIR / "youtube-cookies.txt"
    return default if default.exists() else None


def safe_download(url: str, out_dir: Path) -> SourceVideo:
    try:
        return download(url, out_dir)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc)
        if "not a bot" in msg or "Sign in to confirm" in msg:
            hint = ("Tes cookies YouTube ont peut-être expiré : renvoie un cookies.txt récent dans Comptes."
                    if cookies_path() else
                    "Ajoute tes cookies YouTube dans l'onglet Comptes, ou envoie directement le fichier vidéo.")
            raise BotCheckError(f"YouTube bloque les téléchargements depuis ce serveur. {hint}") from exc
        raise


def local_source(path: Path, title: str) -> SourceVideo:
    """Vidéo envoyée depuis l'interface (sans passer par YouTube)."""
    import json
    import subprocess

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(json.loads(out.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        raise RuntimeError("fichier vidéo illisible") from None
    return SourceVideo(video_id=path.stem, title=title, path=path, duration=duration, url="", uploader="")
