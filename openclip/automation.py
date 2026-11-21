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
    # recherches de tendances : [{query, added_at, seen: [video_id]}]
    "searches": [],
    "search_period": "week",      # today | week | month : vidéos publiées sur cette période
    "search_min_views": 10000,
    "max_new_videos_per_day": 4,  # limite la charge du serveur (chaque vidéo = plusieurs minutes)
    "upload_post_user": "",       # profil Upload-Post de ce compte (si publication via Upload-Post)
    "lives": [],                  # chaînes dont on clippe les lives : [{url, name, added_at}]
    "live_chunk_minutes": 30,     # durée des morceaux enregistrés pendant un live
}
# filtres de recherche YouTube : tri par nombre de vues + période + vidéos uniquement
SEARCH_SP = {"today": "CAMSBAgCEAE%3D", "week": "CAMSBAgDEAE%3D", "month": "CAMSBAgEEAE%3D"}
SEARCH_EVERY = 60 * 60
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
CHECK_EVERY = 20 * 60

_lock = threading.RLock()

# --------------------------------------------------------------------------- comptes (profils)
# Chaque compte publié (ex. @gta6clipvideo, @inspecteurnews) a ses propres sources, réglages,
# heures, file d'attente et connexion TikTok. Le compte « main » garde les fichiers d'origine.
import contextlib
import contextvars
import re as _re

_current = contextvars.ContextVar("openclip_profile", default="main")


def current_profile() -> str:
    return _current.get()


@contextlib.contextmanager
def use_profile(pid: str):
    token = _current.set(pid or "main")
    try:
        yield
    finally:
        _current.reset(token)


def profile_dir(pid: str | None = None) -> Path:
    pid = pid or _current.get()
    return config.HOME_DIR if pid == "main" else config.HOME_DIR / "profiles" / pid


def list_profiles() -> list[dict]:
    saved = _read(config.HOME_DIR / "profiles.json", [])
    if not any(p["id"] == "main" for p in saved):
        saved.insert(0, {"id": "main", "name": "Compte principal"})
    return saved


def add_profile(name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("donne un nom au compte (ex. @inspecteurnews)")
    pid = _re.sub(r"[^a-z0-9]+", "-", name.lower().lstrip("@")).strip("-")[:30] or "compte"
    with _lock:
        profiles = list_profiles()
        base, i = pid, 2
        while any(p["id"] == pid for p in profiles):
            pid, i = f"{base}-{i}", i + 1
        profiles.append({"id": pid, "name": name})
        _write(config.HOME_DIR / "profiles.json", profiles)
    return {"id": pid, "name": name}


def rename_profile(pid: str, name: str) -> None:
    with _lock:
        profiles = list_profiles()
        for p in profiles:
            if p["id"] == pid:
                p["name"] = name.strip() or p["name"]
        _write(config.HOME_DIR / "profiles.json", profiles)


def delete_profile(pid: str) -> None:
    if pid == "main":
        raise ValueError("le compte principal ne peut pas être supprimé")
    import shutil
    with _lock:
        _write(config.HOME_DIR / "profiles.json", [p for p in list_profiles() if p["id"] != pid])
        shutil.rmtree(profile_dir(pid), ignore_errors=True)


def _files() -> tuple[Path, Path]:
    d = profile_dir()
    return d / "automation.json", d / "queue.json"


def _state_file() -> Path:
    return profile_dir() / "automation-state.json"


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
            if key in DEFAULTS and key not in {"channels", "searches", "lives"}:
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


def add_live(url: str) -> dict:
    from .live import channel_name
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ValueError("colle le lien de la chaîne (twitch.tv/nom, youtube.com/@nom, kick.com/nom)")
    with _lock:
        data = load_settings()
        if any(l["url"] == url for l in data["lives"]):
            raise ValueError("live déjà suivi")
        item = {"url": url, "name": channel_name(url), "added_at": time.time()}
        data["lives"].append(item)
        _write(_files()[0], data)
        return item


def remove_live(url: str) -> None:
    with _lock:
        data = load_settings()
        data["lives"] = [l for l in data["lives"] if l["url"] != url]
        _write(_files()[0], data)


def remove_channel(channel_id: str) -> None:
    with _lock:
        data = load_settings()
        data["channels"] = [c for c in data["channels"] if c["channel_id"] != channel_id]
        _write(_files()[0], data)


# --------------------------------------------------------------------------- recherches (tendances)

def search_videos(query: str, period: str = "week", limit: int = 30) -> list[dict]:
    """Vidéos YouTube récentes les plus vues pour une recherche (sans les télécharger)."""
    import urllib.parse

    import yt_dlp

    from .download import cookies_path

    url = (f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
           f"&sp={SEARCH_SP.get(period, SEARCH_SP['week'])}")
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "playlistend": limit}
    cookies = cookies_path()
    if cookies:
        opts["cookiefile"] = str(cookies)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        if not vid or len(vid) != 11:
            continue
        out.append({"id": vid, "title": e.get("title") or "", "duration": e.get("duration") or 0,
                    "views": e.get("view_count") or 0, "channel": e.get("channel") or e.get("uploader") or "",
                    "live": e.get("live_status") in {"is_live", "is_upcoming"},
                    "url": f"https://www.youtube.com/watch?v={vid}"})
    return out


def add_search(query: str) -> dict:
    query = query.strip()
    if len(query) < 2:
        raise ValueError("recherche trop courte")
    with _lock:
        data = load_settings()
        if any(s["query"].lower() == query.lower() for s in data["searches"]):
            raise ValueError("recherche déjà suivie")
        item = {"query": query, "added_at": time.time(), "seen": []}
        data["searches"].append(item)
        _write(_files()[0], data)
        return item


def remove_search(query: str) -> None:
    with _lock:
        data = load_settings()
        data["searches"] = [s for s in data["searches"] if s["query"] != query]
        _write(_files()[0], data)


def pick_trending(videos: list[dict], seen: set, min_views: int, max_minutes: float, count: int) -> list[dict]:
    """Les vidéos les plus vues, pas encore traitées, de 2 min à `max_minutes`."""
    # durée inconnue = souvent un live en cours : on l'écarte
    ok = [v for v in videos if v["id"] not in seen and v["views"] >= min_views and not v.get("live")
          and v["duration"] and 120 <= v["duration"] <= max_minutes * 60]
    return sorted(ok, key=lambda v: -v["views"])[:count]


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


def _enqueue_for(pid: str, project_dir: Path, min_score: float) -> int:
    """Appelé quand les clips sont prêts (dans un autre fil) : file d'attente du bon compte."""
    with use_profile(pid):
        return enqueue_project(project_dir, min_score)


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
        # horodatages par compte
        self.last_check: dict[str, float] = {}
        self.last_search: dict[str, float] = {}
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
        if time.time() - getattr(self, "_last_cleanup", 0) > 3600:
            self._last_cleanup = time.time()
            try:
                from .pipeline import cleanup_sources
                cleanup_sources(self.out_root)
            except Exception:
                log.exception("nettoyage du disque")
        active = 0
        for profile in list_profiles():
            with use_profile(profile["id"]):
                try:
                    active += self._tick_profile(now)
                except Exception:
                    log.exception("automatisation du compte %s", profile["id"])
        self.status = "actif" if active else "désactivé"

    def _tick_profile(self, now: datetime | None) -> int:
        pid = current_profile()
        cfg = load_settings()
        if not cfg["enabled"]:
            return 0
        if time.time() - self.last_check.get(pid, 0) >= CHECK_EVERY:
            self.check_channels(cfg)
        if cfg["searches"] and time.time() - self.last_search.get(pid, 0) >= SEARCH_EVERY:
            self.check_searches(cfg)
        from zoneinfo import ZoneInfo

        when = now or datetime.now(ZoneInfo(cfg["timezone"]))
        path = _state_file()
        key = due_slot(when, cfg["slots"], _read(path, {}).get("last_slot"))
        if key:
            with _lock:
                _write(path, {**_read(path, {}), "last_slot": key})
            self.publish_next(cfg)
        return 1

    def check_channels(self, cfg: dict | None = None) -> int:
        cfg = cfg or load_settings()
        self.last_check[current_profile()] = time.time()
        found = 0
        for ch in cfg["channels"]:
            try:
                feed = fetch_feed(ch["channel_id"])
            except Exception as exc:
                log.warning("flux de %s illisible : %s", ch.get("name"), exc)
                continue
            new = [v for v in feed["videos"] if v["id"] not in set(ch.get("seen", []))]
            for video in reversed(new):  # les plus anciennes d'abord
                if self._budget_left(cfg) <= 0:
                    break  # on reprendra demain : la vidéo n'est pas marquée comme vue
                self._remember(ch["channel_id"], video["id"])
                if "/shorts/" in video["url"]:
                    continue
                found += 1
                self._submit(video["url"], cfg)
        return found

    # -- plafond de vidéos traitées par jour (le serveur n'a que 2 cœurs)
    def _budget_left(self, cfg: dict) -> int:
        state = _read(_state_file(), {})
        today = time.strftime("%Y-%m-%d")
        used = state.get("processed", {}).get(today, 0)
        return max(0, int(cfg["max_new_videos_per_day"]) - used)

    def _spend(self) -> None:
        path = _state_file()
        with _lock:
            state = _read(path, {})
            today = time.strftime("%Y-%m-%d")
            state["processed"] = {today: state.get("processed", {}).get(today, 0) + 1}
            _write(path, state)

    def _submit(self, url: str, cfg: dict) -> None:
        self._spend()
        self.submit(url, {
            "num_clips": cfg["clips_per_video"], "preset": cfg["preset"],
            "caption_style": cfg["caption_style"], "crop_mode": cfg["crop_mode"],
            "max_video_minutes": cfg["max_video_minutes"],
        }, lambda project_dir, ms=cfg["min_score"], pid=current_profile(): project_dir and _enqueue_for(pid, project_dir, ms))

    def check_searches(self, cfg: dict | None = None) -> int:
        """Cherche les vidéos tendance pour chaque recherche suivie et lance les meilleures."""
        cfg = cfg or load_settings()
        self.last_search[current_profile()] = time.time()
        found = 0
        for item in cfg["searches"]:
            budget = self._budget_left(cfg)
            if budget <= 0:
                break
            try:
                videos = search_videos(item["query"], cfg["search_period"])
            except Exception as exc:
                log.warning("recherche « %s » impossible : %s", item["query"], exc)
                continue
            seen = set(item.get("seen", []))
            for ch in cfg["channels"]:
                seen |= set(ch.get("seen", []))
            for video in pick_trending(videos, seen, int(cfg["search_min_views"]),
                                       float(cfg["max_video_minutes"]), min(budget, 2)):
                self._remember_search(item["query"], video["id"])
                self._submit(video["url"], cfg)
                found += 1
        return found

    def _remember_search(self, query: str, video_id: str) -> None:
        with _lock:
            data = load_settings()
            for s_ in data["searches"]:
                if s_["query"] == query:
                    s_["seen"] = (s_.get("seen", []) + [video_id])[-500:]
            _write(_files()[0], data)

    def _remember(self, channel_id: str, video_id: str) -> None:
        with _lock:
            data = load_settings()
            for c in data["channels"]:
                if c["channel_id"] == channel_id:
                    c["seen"] = (c.get("seen", []) + [video_id])[-200:]
            _write(_files()[0], data)

    def publish_next(self, cfg: dict | None = None) -> dict | None:
        cfg = cfg or load_settings()
        if not cfg["targets"]:  # partage manuel uniquement : les clips restent « à partager »
            return None
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
