"""Mode automatique : suivre des chaînes YouTube, créer les clips et les publier à heures fixes.

- Toutes les 20 minutes, OpenClip lit le flux RSS public de chaque chaîne suivie.
- Chaque nouvelle vidéo (publiée après l'ajout de la chaîne) est transformée en clips.
- Les clips dont le score dépasse le minimum entrent dans une file d'attente, meilleurs d'abord.
- À chaque créneau horaire choisi (ex. 12:00, 18:00, 21:00), le meilleur clip en attente est
  publié sur les comptes connectés (TikTok, YouTube).

Tout est enregistré dans ~/.openclip/automation.json et ~/.openclip/queue.json.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import config

log = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": False,
    "channels": [],          # [{url, channel_id, name, added_at, seen: [video_id]}]
    "clips_per_video": 3,
    "min_score": 55,
    "preset": "shorts",
    "caption_style": "pop",
    "crop_mode": "face",
    "targets": ["tiktok", "youtube"],
    "slots": ["12:00", "18:00", "21:00"],
    "timezone": "Europe/Paris",
    "max_video_minutes": 90,
}
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
CHECK_EVERY = 20 * 60

_lock = threading.RLock()


def _files() -> tuple[Path, Path]:
    return config.HOME_DIR / "automation.json", config.HOME_DIR / "queue.json"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def load_settings() -> dict:
    with _lock:
        return {**DEFAULTS, **_read(_files()[0], {})}


def save_settings(changes: dict) -> dict:
    with _lock:
        data = load_settings()
        for key, value in changes.items():
            if key in DEFAULTS and key != "channels":
                data[key] = value
        data["slots"] = sorted({s for s in data["slots"] if _valid_slot(s)})
        _write(_files()[0], data)
        return data


def _valid_slot(s: str) -> bool:
    try:
        h, m = s.split(":")
        return 0 <= int(h) < 24 and 0 <= int(m) < 60 and len(m) == 2
    except (ValueError, AttributeError):
        return False


def load_queue() -> list[dict]:
    with _lock:
        return _read(_files()[1], [])


def _save_queue(queue: list[dict]) -> None:
    _write(_files()[1], queue)


# --------------------------------------------------------------------------- chaînes

def resolve_channel(url: str) -> dict:
    """Trouve l'identifiant (UC…) et le nom d'une chaîne à partir de son lien (@nom, /channel/…)."""
    import re

    m = re.search(r"(UC[\w-]{22})", url)
    if m:
        feed = fetch_feed(m.group(1))
        return {"channel_id": m.group(1), "name": feed["name"]}
    import yt_dlp

    from .download import cookies_path

    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlist_items": "1"}
    cookies = cookies_path()
    if cookies:
        opts["cookiefile"] = str(cookies)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    cid = info.get("channel_id") or (info.get("id") if str(info.get("id", "")).startswith("UC") else None)
    if not cid:
        raise ValueError("chaîne introuvable : colle le lien de la chaîne (youtube.com/@nom)")
    return {"channel_id": cid, "name": info.get("channel") or info.get("uploader") or info.get("title") or cid}


def fetch_feed(channel_id: str) -> dict:
    with urllib.request.urlopen(FEED.format(channel_id), timeout=20) as r:
        root = ET.fromstring(r.read())
    name = root.findtext("a:title", default=channel_id, namespaces=NS)
    videos = []
    for entry in root.findall("a:entry", NS):
        videos.append({
            "id": entry.findtext("yt:videoId", namespaces=NS),
            "title": entry.findtext("a:title", default="", namespaces=NS),
            "published": entry.findtext("a:published", default="", namespaces=NS),
            "url": f"https://www.youtube.com/watch?v={entry.findtext('yt:videoId', namespaces=NS)}",
        })
    return {"name": name, "videos": videos}


def add_channel(url: str) -> dict:
    info = resolve_channel(url)
    with _lock:
        data = load_settings()
        if any(c["channel_id"] == info["channel_id"] for c in data["channels"]):
            raise ValueError("chaîne déjà suivie")
        # les vidéos déjà en ligne sont ignorées : on ne traite que les nouvelles
        try:
            seen = [v["id"] for v in fetch_feed(info["channel_id"])["videos"]]
        except OSError:
            seen = []
        channel = {"url": url, **info, "added_at": time.time(), "seen": seen}
        data["channels"].append(channel)
        _write(_files()[0], data)
        return channel


def remove_channel(channel_id: str) -> None:
    with _lock:
        data = load_settings()
        data["channels"] = [c for c in data["channels"] if c["channel_id"] != channel_id]
        _write(_files()[0], data)


# --------------------------------------------------------------------------- file d'attente

def enqueue_project(project_dir: Path, min_score: float) -> int:
    manifest = json.loads((project_dir / "clips.json").read_text())
    with _lock:
        queue = load_queue()
        known = {(q["project"], q["clip"]) for q in queue}
        added = 0
        for i, clip in enumerate(manifest["clips"], 1):
            if clip.get("score", 0) < min_score or (project_dir.name, i) in known:
                continue
            queue.append({"project": project_dir.name, "clip": i, "title": clip["title"],
                          "score": clip.get("score", 0), "status": "pending", "added_at": time.time(),
                          "source": manifest["source"].get("title", "")})
            added += 1
        _save_queue(queue)
    return added


def next_pending() -> dict | None:
    pending = [q for q in load_queue() if q["status"] == "pending"]
    return max(pending, key=lambda q: (q["score"], -q["added_at"])) if pending else None


def mark(item: dict, **changes) -> None:
    with _lock:
        queue = load_queue()
        for q in queue:
            if q["project"] == item["project"] and q["clip"] == item["clip"]:
                q.update(changes)
        _save_queue(queue)


def remove_from_queue(project: str, clip: int) -> None:
    with _lock:
        _save_queue([q for q in load_queue() if not (q["project"] == project and q["clip"] == clip)])


# --------------------------------------------------------------------------- planification

def due_slot(now: datetime, slots: list[str], last_run: str | None) -> str | None:
    """Renvoie l'identifiant « AAAA-MM-JJ HH:MM » du créneau à exécuter maintenant, s'il y en a un.

    Un créneau est dû s'il est passé depuis moins de 2 heures et n'a pas déjà été traité.
    """
    for slot in sorted(slots, reverse=True):
        h, m = map(int, slot.split(":"))
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if at <= now < at + timedelta(hours=2):
            key = at.strftime("%Y-%m-%d %H:%M")
            return None if key == last_run else key
    return None


class Scheduler:
    """Boucle de fond : nouvelles vidéos -> clips -> file d'attente -> publication aux créneaux."""

    def __init__(self, submit: Callable[[str, dict, Callable], None],
                 publish: Callable[[Path, int, str], dict], out_root: Path):
        self.submit = submit          # submit(url, options, on_done(project_dir | None))
        self.publish = publish        # publish(project_dir, clip_index, target) -> résultat
        self.out_root = out_root
        self.last_check = 0.0
        self.last_slot: str | None = _read(config.HOME_DIR / "automation-state.json", {}).get("last_slot")
        self.status = "arrêté"
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="openclip-auto").start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self.tick()
            except Exception:  # la boucle ne doit jamais mourir
                log.exception("automatisation")

    def tick(self, now: datetime | None = None) -> None:
        cfg = load_settings()
        if not cfg["enabled"]:
            self.status = "désactivé"
            return
        if time.time() - self.last_check >= CHECK_EVERY:
            self.check_channels(cfg)
        from zoneinfo import ZoneInfo

        now = now or datetime.now(ZoneInfo(cfg["timezone"]))
        key = due_slot(now, cfg["slots"], self.last_slot)
        if key:
            self.last_slot = key
            _write(config.HOME_DIR / "automation-state.json", {"last_slot": key})
            self.publish_next(cfg)
        self.status = "actif"

    def check_channels(self, cfg: dict | None = None) -> int:
        cfg = cfg or load_settings()
        self.last_check = time.time()
        found = 0
        for ch in cfg["channels"]:
            try:
                feed = fetch_feed(ch["channel_id"])
            except Exception as exc:
                log.warning("flux de %s illisible : %s", ch.get("name"), exc)
                continue
            new = [v for v in feed["videos"] if v["id"] not in set(ch.get("seen", []))]
            for video in reversed(new):  # les plus anciennes d'abord
                self._remember(ch["channel_id"], video["id"])
                if "/shorts/" in video["url"]:
                    continue
                found += 1
                self.submit(video["url"], {
                    "num_clips": cfg["clips_per_video"], "preset": cfg["preset"],
                    "caption_style": cfg["caption_style"], "crop_mode": cfg["crop_mode"],
                    "max_video_minutes": cfg["max_video_minutes"],
                }, lambda project_dir, ms=cfg["min_score"]: project_dir and enqueue_project(project_dir, ms))
        return found

    def _remember(self, channel_id: str, video_id: str) -> None:
        with _lock:
            data = load_settings()
            for c in data["channels"]:
                if c["channel_id"] == channel_id:
                    c["seen"] = (c.get("seen", []) + [video_id])[-200:]
            _write(_files()[0], data)

    def publish_next(self, cfg: dict | None = None) -> dict | None:
        cfg = cfg or load_settings()
        item = next_pending()
        if not item:
            return None
        results, errors = {}, {}
        for target in cfg["targets"]:
            try:
                results[target] = self.publish(self.out_root / item["project"], item["clip"], target)
            except Exception as exc:
                errors[target] = str(exc)
        mark(item, status="published" if results else "failed", published_at=time.time(),
             results=results, errors=errors)
        return {**item, "results": results, "errors": errors}
