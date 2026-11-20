"""Clipper des lives pendant qu'ils ont lieu (Twitch, YouTube, Kick…).

Toutes les 5 minutes, OpenClip regarde si les chaînes suivies sont en direct. Pendant un
live, il enregistre le flux par morceaux (30 min par défaut) ; chaque morceau terminé est
envoyé au montage comme une vidéo normale (transcription, meilleurs moments, caméra + jeu),
puis ses clips arrivent dans « À partager » et la file d'attente du compte.

Les morceaux sont gardés quelques jours (pour pouvoir refaire un clip) puis supprimés.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

CHECK_EVERY = 5 * 60          # vérification « en direct ? »
KEEP_DAYS = 3                 # durée de conservation des morceaux enregistrés
MIN_CHUNK_SECONDS = 120       # un morceau plus court (fin de live) n'est pas monté
MAX_BACKLOG = 2               # morceaux en attente de montage par chaîne avant de sauter un enregistrement


def live_url(url: str) -> str:
    """Lien à interroger : youtube.com/@nom -> youtube.com/@nom/live ; Twitch/Kick tels quels."""
    url = url.strip().rstrip("/")
    if re.search(r"youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+)$", url):
        return url + "/live"
    return url


def channel_name(url: str) -> str:
    m = re.search(r"(?:@|twitch\.tv/|kick\.com/|channel/|c/)([^/?#]+)", url)
    return m.group(1) if m else url


def _ydl_opts() -> dict:
    from .download import cookies_path

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
            "format": "best[height<=720][protocol^=m3u8]/best[height<=720]/best"}
    deno = shutil.which("deno") or str(Path(sys.executable).with_name("deno"))
    if Path(deno).exists():
        opts["js_runtimes"] = {"deno": {"path": deno}}
    cookies = cookies_path()
    if cookies:
        opts["cookiefile"] = str(cookies)
    return opts


def probe_live(url: str) -> dict | None:
    """Infos du flux si la chaîne est en direct maintenant, sinon None."""
    import yt_dlp

    try:
        with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
            info = ydl.extract_info(live_url(url), download=False)
    except yt_dlp.utils.DownloadError as exc:  # « not currently live », « will begin in… »
        log.debug("%s hors ligne : %s", url, exc)
        return None
    if not info or not (info.get("is_live") or info.get("live_status") == "is_live"):
        return None
    if not info.get("url"):
        return None
    return info


def record(info: dict, out: Path, seconds: int, stop: threading.Event | None = None) -> float:
    """Enregistre `seconds` secondes du flux (copie sans réencodage). Renvoie la durée obtenue."""
    out.parent.mkdir(parents=True, exist_ok=True)
    headers = "".join(f"{k}: {v}\r\n" for k, v in (info.get("http_headers") or {}).items()
                      if k.lower() in {"user-agent", "referer", "origin", "cookie"})
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if headers and str(info["url"]).startswith("http"):
        cmd += ["-headers", headers]
    cmd += ["-i", info["url"], "-t", str(seconds), "-map", "0:v:0?", "-map", "0:a:0?",
            "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while proc.poll() is None:
        if stop is not None and stop.wait(2):
            proc.terminate()  # arrêt demandé : on garde ce qui a été enregistré
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
            break
    return _duration(out)


def _duration(path: Path) -> float:
    if not path.exists() or path.stat().st_size < 10_000:
        return 0.0
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                          "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


class LiveManager:
    """Surveille les lives suivis de tous les comptes et enregistre/monte les morceaux."""

    def __init__(self, out_root: Path, submit_file: Callable[[Path, str, dict, str], None],
                 max_parallel: int = 2):
        # submit_file(fichier, titre, options du compte, id du compte)
        self.out_root = out_root
        self.submit_file = submit_file
        self.max_parallel = max_parallel
        self.state: dict[str, dict] = {}   # clé « compte|url » -> état affiché dans l'interface
        self.last_check: dict[str, float] = {}
        self.backlog: dict[str, int] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ boucle
    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="openclip-live").start()

    def stop(self) -> None:
        self._stop.set()
        for ev in self._stops.values():
            ev.set()

    def _loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self.tick()
            except Exception:
                log.exception("surveillance des lives")

    def tick(self) -> None:
        from . import automation

        wanted = set()
        for profile in automation.list_profiles():
            with automation.use_profile(profile["id"]):
                cfg = automation.load_settings()
            if not cfg["enabled"]:
                continue
            for ch in cfg.get("lives", []):
                key = f"{profile['id']}|{ch['url']}"
                wanted.add(key)
                self._maybe_record(key, ch, cfg, profile["id"])
        # chaînes retirées ou compte désactivé : on arrête l'enregistrement en cours
        for key in list(self._threads):
            if key not in wanted:
                self._stops[key].set()
        self._cleanup()

    def _maybe_record(self, key: str, ch: dict, cfg: dict, profile_id: str) -> None:
        with self._lock:
            busy = key in self._threads and self._threads[key].is_alive()
            running = sum(t.is_alive() for t in self._threads.values())
        if busy:
            return
        if time.time() - self.last_check.get(key, 0) < CHECK_EVERY:
            return
        self.last_check[key] = time.time()
        if self.backlog.get(key, 0) >= MAX_BACKLOG:
            self._set(key, status="busy", message="Montage en retard : enregistrement en pause")
            return
        if running >= self.max_parallel:
            self._set(key, status="waiting", message="Trop de lives en même temps sur ce serveur")
            return
        info = probe_live(ch["url"])
        if not info:
            self._set(key, status="offline", message="Hors ligne", title="")
            return
        stop = threading.Event()
        t = threading.Thread(target=self._record_chunk, args=(key, ch, cfg, profile_id, info, stop),
                             daemon=True, name=f"live-{channel_name(ch['url'])}")
        with self._lock:
            self._threads[key], self._stops[key] = t, stop
        t.start()

    def _record_chunk(self, key: str, ch: dict, cfg: dict, profile_id: str, info: dict,
                      stop: threading.Event) -> None:
        minutes = float(cfg.get("live_chunk_minutes", 30))
        name = ch.get("name") or channel_name(ch["url"])
        started = datetime.now()
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "live"
        out = self.out_root / "_sources" / "live" / f"live-{slug}-{started:%Y%m%d-%H%M}.mp4"
        self._set(key, status="recording", since=time.time(), title=info.get("title", ""),
                  message=f"🔴 En direct : enregistrement de {minutes:g} min en cours")
        try:
            got = record(info, out, int(minutes * 60), stop)
        except Exception as exc:
            log.warning("enregistrement de %s impossible : %s", name, exc)
            self._set(key, status="error", message=f"Enregistrement impossible : {exc}")
            return
        self.last_check[key] = 0  # toujours en direct ? on enchaîne tout de suite sur le morceau suivant
        if got < MIN_CHUNK_SECONDS:
            out.unlink(missing_ok=True)
            self._set(key, status="offline", message="Live terminé")
            return
        title = f"Live {name} · {started:%d/%m %H:%M}"
        if info.get("title"):
            title += f" · {info['title']}"[:80]
        self.backlog[key] = self.backlog.get(key, 0) + 1

        options = {"num_clips": cfg["clips_per_video"], "preset": cfg["preset"],
                   "caption_style": cfg["caption_style"], "crop_mode": cfg["crop_mode"],
                   "_on_finished": lambda: self._done(key)}
        self.submit_file(out, title, options, profile_id)
        self._set(key, status="processing", message=f"Morceau de {got / 60:.0f} min envoyé au montage",
                  chunks=self.state.get(key, {}).get("chunks", 0) + 1, last_chunk_at=time.time())

    def _done(self, key: str) -> None:
        self.backlog[key] = max(0, self.backlog.get(key, 0) - 1)

    def _set(self, key: str, **values) -> None:
        with self._lock:
            self.state[key] = {**self.state.get(key, {}), **values, "updated_at": time.time()}

    def status(self, profile_id: str) -> dict[str, dict]:
        prefix = f"{profile_id}|"
        return {k[len(prefix):]: v for k, v in self.state.items() if k.startswith(prefix)}

    def _cleanup(self) -> None:
        folder = self.out_root / "_sources" / "live"
        if not folder.exists():
            return
        limit = time.time() - KEEP_DAYS * 86400
        for f in folder.glob("live-*.mp4"):
            if f.stat().st_mtime < limit:
                f.unlink(missing_ok=True)
