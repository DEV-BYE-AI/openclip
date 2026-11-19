"""Interface web OpenClip (FastAPI). Lancer avec `openclip web`."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import PRESETS, ClipSettings, Settings
from ..subtitles import STYLES

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class JobIn(BaseModel):
    url: str
    num_clips: int = Field(5, ge=1, le=20)
    preset: str = "shorts"
    min_seconds: float | None = None
    max_seconds: float | None = None
    crop_mode: str = "face"
    caption_style: str = "pop"
    language: str | None = None
    jump_cuts: bool = True
    progress_bar: bool = True
    subtitles: bool = True
    auto_publish: list[str] = []


class ClipPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    hook: str | None = None
    style: str | None = None
    keywords: list[str] | None = None
    start: float | None = None
    end: float | None = None


class PublishIn(BaseModel):
    target: str
    tiktok_mode: str = "direct"
    privacy: str | None = None


@dataclass
class Job:
    id: str
    url: str
    kind: str = "create"
    stage: str = "queued"
    progress: float = 0.0
    message: str = "En file d'attente"
    project: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)


def create_app(out_root: Path = Path("output"), settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    out_root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="OpenClip")
    jobs: dict[str, Job] = {}
    lock = threading.Lock()
    heavy = ThreadPoolExecutor(max_workers=1)   # téléchargement / whisper / ffmpeg
    light = ThreadPoolExecutor(max_workers=2)   # uploads
    oauth_states: set[str] = set()

    def project_dir(pid: str) -> Path:
        d = (out_root / pid).resolve()
        if d.parent != out_root.resolve() or not (d / "clips.json").exists():
            raise HTTPException(404, "projet introuvable")
        return d

    def update(job: Job, **kw) -> None:
        with lock:
            for k, v in kw.items():
                setattr(job, k, v)

    # ---------- création ----------
    def run_create(job: Job, body: JobIn) -> None:
        from ..pipeline import make_clips, publish

        lo, hi = PRESETS.get(body.preset, PRESETS["shorts"])
        s = Settings()
        s.clip = ClipSettings(
            min_seconds=body.min_seconds or lo, max_seconds=body.max_seconds or hi,
            num_clips=body.num_clips, crop_mode=body.crop_mode, subtitles=body.subtitles,
            language=body.language or None, caption_style=body.caption_style,
            jump_cuts=body.jump_cuts, progress_bar=body.progress_bar,
        )
        try:
            out = make_clips(body.url, s, out_root,
                             lambda st, fr, msg: update(job, stage=st, progress=fr, message=msg))
            update(job, project=out.name)
            if body.auto_publish:
                update(job, stage="publish", message="Publication automatique")
                publish(out, s, body.auto_publish)
            update(job, stage="done", progress=1.0)
        except Exception as exc:
            log.error("job %s : %s", job.id, traceback.format_exc())
            update(job, stage="error", error=str(exc), message="Échec")

    @app.post("/api/jobs")
    def create_job(body: JobIn):
        if not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "lien invalide")
        job = Job(id=uuid.uuid4().hex[:10], url=body.url)
        jobs[job.id] = job
        heavy.submit(run_create, job, body)
        return job.__dict__

    @app.get("/api/jobs")
    def list_jobs():
        with lock:
            return sorted((j.__dict__.copy() for j in jobs.values()),
                          key=lambda j: -j["created_at"])

    # ---------- projets ----------
    @app.get("/api/projects")
    def list_projects():
        items = []
        for f in out_root.glob("*/clips.json"):
            try:
                m = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            clips = m.get("clips", [])
            items.append({
                "id": f.parent.name,
                "title": m["source"].get("title"),
                "uploader": m["source"].get("uploader"),
                "url": m["source"].get("url"),
                "created_at": m.get("created_at", f.stat().st_mtime),
                "count": len(clips),
                "best": max((c.get("score", 0) for c in clips), default=0),
                "published": sum(1 for c in clips if c.get("uploads")),
                "cover": f"/media/{f.parent.name}/{Path(clips[0]['file']).stem}.jpg" if clips else None,
            })
        return sorted(items, key=lambda p: -p["created_at"])

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        d = project_dir(pid)
        m = json.loads((d / "clips.json").read_text())
        m["id"] = pid
        m["source"].pop("path", None)
        m["source"].pop("transcript", None)
        return m

    @app.patch("/api/projects/{pid}/clips/{n}")
    def patch_clip(pid: str, n: int, body: ClipPatch):
        from ..pipeline import _save, load_manifest

        d = project_dir(pid)
        m = load_manifest(d)
        if not 1 <= n <= len(m["clips"]):
            raise HTTPException(404, "clip introuvable")
        changes = body.model_dump(exclude_none=True)
        visual = {k: v for k, v in changes.items() if k in {"hook", "style", "keywords", "start", "end"}}
        clip = m["clips"][n - 1]
        clip.update({k: v for k, v in changes.items() if k not in visual})
        _save(d, m)
        def changed(k, v):
            if k in {"start", "end"}:
                return abs(float(clip.get(k, 0)) - float(v)) > 0.05
            return clip.get(k) != v

        visual = {k: v for k, v in visual.items() if changed(k, v)}
        if visual:
            job = Job(id=uuid.uuid4().hex[:10], url=pid, kind="rerender", project=pid,
                      message=f"Nouveau montage du clip {n}")
            jobs[job.id] = job

            def run():
                from ..pipeline import rerender
                try:
                    update(job, stage="render", progress=0.3)
                    rerender(d, n, visual)
                    update(job, stage="done", progress=1.0, message="Clip mis à jour")
                except Exception as exc:
                    update(job, stage="error", error=str(exc), message="Échec du montage")

            heavy.submit(run)
            return {"job": job.__dict__}
        return {"clip": clip}

    @app.post("/api/projects/{pid}/clips/{n}/publish")
    def publish_clip(pid: str, n: int, body: PublishIn):
        from ..pipeline import publish_one

        d = project_dir(pid)
        job = Job(id=uuid.uuid4().hex[:10], url=pid, kind="publish", project=pid,
                  stage="publish", message=f"Envoi du clip {n} vers {body.target}")
        jobs[job.id] = job

        def run():
            try:
                publish_one(d, n, settings, body.target, tiktok_mode=body.tiktok_mode,
                            privacy=body.privacy)
                update(job, stage="done", progress=1.0, message=f"Publié sur {body.target}")
            except Exception as exc:
                update(job, stage="error", error=str(exc), message="Échec de la publication")

        light.submit(run)
        return job.__dict__

    @app.get("/api/projects/{pid}/clips/{n}/download")
    def download_clip(pid: str, n: int):
        d = project_dir(pid)
        m = json.loads((d / "clips.json").read_text())
        clip = m["clips"][n - 1]
        safe = "".join(c for c in clip["title"] if c.isalnum() or c in " -_")[:60].strip() or "clip"
        return FileResponse(d / clip["file"], media_type="video/mp4", filename=f"{safe}.mp4")

    # ---------- comptes ----------
    @app.get("/api/meta")
    def meta():
        from ..uploaders import tiktok, youtube
        return {
            "accounts": {
                "claude": bool(os.environ.get("ANTHROPIC_API_KEY")),
                "tiktok": tiktok.is_connected(),
                "tiktok_configured": bool(settings.tiktok_client_key),
                "youtube": youtube.is_connected(),
                "youtube_configured": Path(settings.youtube_client_secrets).exists(),
            },
            "styles": {k: v.label for k, v in STYLES.items()},
            "presets": PRESETS,
        }

    @app.get("/auth/tiktok")
    def auth_tiktok(request: Request):
        from ..uploaders import tiktok
        state = secrets.token_urlsafe(16)
        oauth_states.add(state)
        redirect = str(request.url_for("auth_tiktok_callback"))
        try:
            return RedirectResponse(tiktok.authorize_url(settings, state, redirect))
        except tiktok.TikTokError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/auth/tiktok/callback", name="auth_tiktok_callback")
    def auth_tiktok_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        from ..uploaders import tiktok
        if error or state not in oauth_states:
            raise HTTPException(400, error or "state OAuth invalide")
        oauth_states.discard(state)
        tiktok.exchange_code(settings, code, str(request.url_for("auth_tiktok_callback")))
        return RedirectResponse("/#/comptes")

    @app.post("/api/auth/youtube")
    def auth_youtube():
        from ..uploaders import youtube
        # ouvre le navigateur sur la machine qui fait tourner OpenClip
        light.submit(youtube.login, settings)
        return {"started": True}

    app.mount("/media", StaticFiles(directory=out_root), name="media")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    return app


def serve(host: str, port: int, out_root: Path) -> None:
    import uvicorn

    print(f"OpenClip → http://{host}:{port}")
    uvicorn.run(create_app(out_root), host=host, port=port, log_level="warning")
