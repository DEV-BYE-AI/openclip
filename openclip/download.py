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
    cookies = os.environ.get("YTDLP_COOKIES")
    if cookies:
        opts["cookiefile"] = cookies
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
