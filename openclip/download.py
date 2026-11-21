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


def _format() -> str:
    """Qualité de la vidéo source. SOURCE_MAX_HEIGHT=1440 (ou 2160) donne une caméra de streamer
    bien plus nette dans les clips « caméra + jeu », au prix de fichiers ~2x plus lourds et
    d'un montage plus lent. Par défaut 1080 : H.264 privilégié (rapide à décoder)."""
    h = int(os.environ.get("SOURCE_MAX_HEIGHT", "1080"))
    if h <= 1080:
        return (f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
                f"/bv*[height<={h}]+ba/b[height<={h}]/b")
    # au-delà de 1080p, YouTube ne propose que VP9/AV1 : on prend la meilleure définition
    return f"bv*[height<={h}]+ba/b[height<={h}]/b"


class NotDownloadable(RuntimeError):
    pass


def check_downloadable(info: dict) -> None:
    """Refuse les lives en cours / à venir et les vidéos trop longues (MAX_SOURCE_HOURS, 4 h)."""
    status = info.get("live_status") or ("is_live" if info.get("is_live") else "")
    if status in {"is_live", "is_upcoming", "post_live"}:
        raise NotDownloadable("c'est un live en cours (ou pas encore terminé) : ajoute la chaîne dans "
                              "Auto → Lives suivis, ou réessaie quand la rediffusion sera disponible")
    hours = float(os.environ.get("MAX_SOURCE_HOURS", "4"))
    duration = float(info.get("duration") or 0)
    if duration > hours * 3600:
        raise NotDownloadable(f"vidéo trop longue ({duration / 3600:.1f} h, maximum {hours:g} h)")


def download(url: str, out_dir: Path) -> SourceVideo:
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        # 1080p max, mp4 de préférence (plus rapide à ré-encoder)
        # H.264 (avc1) en priorité : bien plus rapide à décoder que l'AV1 ou le VP9 sur un petit serveur
        "format": _format(),
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
        # on regarde d'abord ce que c'est : un live en cours ne se termine jamais (le fichier
        # grossirait sans fin), et une vidéo de plusieurs heures remplirait le disque
        info = ydl.extract_info(url, download=False, process=False)
        check_downloadable(info)
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
            from .browser_login import cookie_file_status
            cookies = cookies_path()
            logged = cookie_file_status(cookies)["logged_in"] if cookies else False
            hint = ("Tes cookies ne contiennent pas de session YouTube connectée : refais « Se connecter à "
                    "YouTube » dans Comptes, en allant jusqu'à youtube.com avec ta photo de profil visible."
                    if cookies and not logged else
                    "Ta session YouTube a peut-être expiré : touche « Actualiser » dans Comptes, ou reconnecte-toi."
                    if cookies else
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


# --------------------------------------------------------------------------- sous-titres YouTube

def download_captions(url: str, out_dir: Path, video_id: str) -> Path | None:
    """Télécharge les sous-titres automatiques d'origine (piste « -orig ») au format json3.

    Ils contiennent l'horodatage de chaque mot : on évite ainsi la transcription locale,
    qui est lente sur un petit serveur. Renvoie None si la vidéo n'en a pas.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": False,
        "subtitleslangs": [".*-orig"],
        "subtitlesformat": "json3",
        "outtmpl": str(out_dir / f"{video_id}.captions.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    cookies = cookies_path()
    if cookies:
        opts["cookiefile"] = str(cookies)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception:
        return None
    found = sorted(out_dir.glob(f"{video_id}.captions.*json3"))
    return found[0] if found else None


def parse_json3(path: Path):
    """Convertit des sous-titres json3 YouTube en segments (phrases) avec mots horodatés."""
    import json as _json

    from .transcribe import Segment, Word

    data = _json.loads(path.read_text(encoding="utf-8"))
    words: list[Word] = []
    for ev in data.get("events", []):
        segs = ev.get("segs") or []
        if ev.get("aAppend") or not segs:
            continue
        start = ev.get("tStartMs", 0) / 1000
        end = start + ev.get("dDurationMs", 0) / 1000
        pieces = [(s.get("tOffsetMs"), s.get("utf8", "")) for s in segs]
        pieces = [(o, t.replace("\n", " ").strip()) for o, t in pieces if t.strip()]
        if not pieces:
            continue
        if len(pieces) == 1 or all(o is None for o, _ in pieces[1:]):
            # pas d'horodatage par mot : on répartit les mots sur la durée de la ligne
            toks = " ".join(t for _, t in pieces).split()
            step = (end - start) / max(len(toks), 1)
            for i, tok in enumerate(toks):
                words.append(Word(start + i * step, start + (i + 1) * step, tok))
            continue
        times = [start + (o or 0) / 1000 for o, _ in pieces]
        for i, (_, text) in enumerate(pieces):
            w_end = times[i + 1] if i + 1 < len(pieces) else max(end, times[i] + 0.2)
            for tok in text.split():
                words.append(Word(times[i], w_end, tok))
    # On garde l'ordre des lignes : certaines pistes (traductions, sous-titres manuels)
    # ont des lignes qui se chevauchent, et trier tous les mots par heure les mélangerait.
    for a, b in zip(words, words[1:]):
        if b.start < a.start:
            b.start = a.start
        if b.end < b.start:
            b.end = b.start + 0.05
        if a.end > b.start:
            a.end = b.start

    # regroupement en phrases : ponctuation, pause, ou longueur maximale
    segments: list[Segment] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        pause = (nxt.start - w.end) if nxt else 99
        if w.text.endswith((".", "?", "!", "…")) or pause > 0.7 or len(cur) >= 16:
            segments.append(Segment(cur[0].start, cur[-1].end, " ".join(x.text for x in cur), cur))
            cur = []
    return segments


def video_duration(url: str) -> float:
    """Durée d'une vidéo YouTube sans la télécharger (0 si inconnue)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    cookies = cookies_path()
    if cookies:
        opts["cookiefile"] = str(cookies)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False, process=False)
    return float(info.get("duration") or 0)
