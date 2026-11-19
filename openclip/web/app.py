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

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import PRESETS, ClipSettings, Settings
from ..highlights import has_claude_key
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
    max_video_minutes: float | None = None


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
    tiktok_mode: str | None = None
    privacy: str | None = None


class TikTokSettingsIn(BaseModel):
    client_key: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None
    mode: str | None = None


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
    requested: int | None = None
    created_at: float = field(default_factory=time.time)


def create_app(out_root: Path = Path("output"), settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    out_root.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="OpenClip")
    password = os.environ.get("OPENCLIP_PASSWORD", "").strip()
    username = os.environ.get("OPENCLIP_USER", "").strip() or "admin"

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        """Protège toute l'interface par identifiant / mot de passe si OPENCLIP_PASSWORD est défini.

        Seules les pages publiques exigées par TikTok / Google (mentions légales, page de retour
        après autorisation) restent accessibles sans mot de passe.
        """
        if password and not request.url.path.startswith(("/legal/", "/callback/")):
            import base64
            ok = False
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(user.encode(), username.encode()) and \
                        secrets.compare_digest(pwd.encode(), password.encode())
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                return Response("Identifiant requis", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="OpenClip", charset="UTF-8"'})
        return await call_next(request)
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
    uploads: dict[str, tuple[Path, str]] = {}
    uploads_dir = out_root / "_sources" / "uploads"

    def run_create(job: Job, body: JobIn, on_done=None) -> None:
        from ..pipeline import make_clips, publish

        lo, hi = PRESETS.get(body.preset, PRESETS["shorts"])
        s = Settings()
        s.clip = ClipSettings(
            min_seconds=body.min_seconds or lo, max_seconds=body.max_seconds or hi,
            num_clips=body.num_clips, crop_mode=body.crop_mode, subtitles=body.subtitles,
            language=body.language or None, caption_style=body.caption_style,
            jump_cuts=body.jump_cuts, progress_bar=body.progress_bar,
        )
        out = None
        try:
            if body.max_video_minutes and not body.url.startswith("upload:"):
                from ..download import video_duration
                update(job, stage="download", progress=0.01, message="Vérification de la durée")
                minutes = video_duration(body.url) / 60
                if minutes > body.max_video_minutes:
                    raise RuntimeError(f"vidéo trop longue ({minutes:.0f} min > {body.max_video_minutes:.0f})")
            local = uploads.get(body.url[len("upload:"):]) if body.url.startswith("upload:") else None
            out = make_clips(body.url, s, out_root,
                             lambda st, fr, msg: update(job, stage=st, progress=fr, message=msg),
                             local_file=local[0] if local else None,
                             local_title=local[1] if local else "")
            update(job, project=out.name)
            if body.auto_publish:
                update(job, stage="publish", message="Publication automatique")
                publish(out, s, body.auto_publish)
            update(job, stage="done", progress=1.0)
        except Exception as exc:
            log.error("job %s : %s", job.id, traceback.format_exc())
            update(job, stage="error", error=str(exc), message="Échec")
            out = None
        if on_done:
            try:
                on_done(out)
            except Exception:
                log.exception("après le job %s", job.id)

    @app.post("/api/jobs")
    def create_job(body: JobIn):
        label = body.url
        if body.url.startswith("upload:"):
            up = uploads.get(body.url[len("upload:"):])
            if not up:
                raise HTTPException(400, "fichier envoyé introuvable, renvoie-le")
            label = f"📁 {up[1]}"
        elif not body.url.startswith(("http://", "https://")):
            raise HTTPException(400, "lien invalide")
        job = Job(id=uuid.uuid4().hex[:10], url=label, requested=body.num_clips)
        jobs[job.id] = job
        heavy.submit(run_create, job, body)
        return job.__dict__

    @app.post("/api/uploads")
    async def upload_video(request: Request):
        """Reçoit un fichier vidéo brut (corps de la requête), en streaming sur le disque."""
        import urllib.parse

        name = urllib.parse.unquote(request.headers.get("x-filename", "video.mp4"))[:120]
        ext = Path(name).suffix.lower()
        if ext not in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".3gp"}:
            raise HTTPException(400, "format non pris en charge (mp4, mov, mkv, webm…)")
        uploads_dir.mkdir(parents=True, exist_ok=True)
        up_id = "upload-" + uuid.uuid4().hex[:8]
        dest = uploads_dir / f"{up_id}{ext}"
        size = 0
        with dest.open("wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
        if size < 1000:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "fichier vide")
        uploads[up_id] = (dest, Path(name).stem)
        return {"id": up_id, "name": name, "size": size}

    @app.get("/api/cookies")
    def cookies_status():
        from ..browser_login import cookie_file_status
        from ..download import cookies_path
        p = cookies_path()
        info = cookie_file_status(p) if p else {"present": False, "logged_in": False}
        return {**info, "updated_at": p.stat().st_mtime if p else None}

    @app.post("/api/cookies")
    async def save_cookies(request: Request):
        from ..config import HOME_DIR, ensure_dirs
        text = (await request.body()).decode("utf-8", "replace")
        lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        if not any("youtube.com" in ln and ln.count("\t") >= 6 for ln in lines):
            raise HTTPException(400, "ce n'est pas un cookies.txt YouTube (format Netscape attendu)")
        ensure_dirs()
        dest = HOME_DIR / "youtube-cookies.txt"
        dest.write_text(text if text.startswith("# Netscape") else "# Netscape HTTP Cookie File\n" + text)
        dest.chmod(0o600)
        return {"present": True, "updated_at": dest.stat().st_mtime}

    @app.delete("/api/cookies")
    def delete_cookies():
        from ..config import HOME_DIR
        (HOME_DIR / "youtube-cookies.txt").unlink(missing_ok=True)
        return {"present": False}

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
                publish_one(d, n, settings, body.target,
                            tiktok_mode=body.tiktok_mode or settings.tiktok_mode, privacy=body.privacy)
                update(job, stage="done", progress=1.0, message=f"Publié sur {body.target}")
            except Exception as exc:
                update(job, stage="error", error=str(exc), message="Échec de la publication")

        light.submit(run)
        return job.__dict__

    @app.delete("/api/projects/{pid}")
    def remove_project(pid: str):
        """Supprime le projet et ses analyses (transcription, sous-titres) pour repartir de zéro."""
        import shutil
        d = project_dir(pid)
        shutil.rmtree(d)
        for f in (out_root / "_sources").glob(f"{pid}.*"):
            if f.suffix != ".mp4":  # la vidéo reste en cache : pas besoin de la retélécharger
                f.unlink(missing_ok=True)
        from .. import automation
        with automation._lock:
            automation._save_queue([q for q in automation.load_queue() if q["project"] != pid])
        return {"ok": True}

    @app.delete("/api/projects/{pid}/clips/{n}")
    def remove_clip(pid: str, n: int):
        from ..pipeline import delete_clip, load_manifest
        d = project_dir(pid)
        if not 1 <= n <= len(load_manifest(d)["clips"]):
            raise HTTPException(404, "clip introuvable")
        delete_clip(d, n)
        return {"ok": True}

    @app.post("/api/projects/{pid}/more")
    async def more_clips(pid: str, request: Request):
        """Relance l'analyse de la même vidéo pour trouver d'autres moments (ajoutés au projet)."""
        from ..pipeline import load_manifest
        d = project_dir(pid)
        src = load_manifest(d)["source"]
        opts = await request.json()
        body = JobIn(url=src.get("url") or "upload:", **{k: v for k, v in opts.items() if k in JobIn.model_fields and k != "url"})
        if not src.get("url"):  # vidéo envoyée depuis le téléphone : on réutilise le fichier
            path = Path(src.get("path", ""))
            if not path.exists():
                raise HTTPException(400, "le fichier vidéo d'origine n'est plus sur le serveur")
            up_id = path.stem
            uploads[up_id] = (path, src.get("title") or path.stem)
            body.url = "upload:" + up_id
        job = Job(id=uuid.uuid4().hex[:10], url=src.get("title") or pid, project=pid,
                  message="Recherche d'autres moments", requested=body.num_clips)
        jobs[job.id] = job
        heavy.submit(run_create, job, body)
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
        settings.reload_credentials()
        return {
            "accounts": {
                "claude": has_claude_key(),
                "tiktok": tiktok.is_connected(),
                "tiktok_configured": bool(settings.tiktok_client_key and settings.tiktok_client_secret
                                          and settings.tiktok_redirect_uri.startswith("https://")),
                "tiktok_client_key": settings.tiktok_client_key,
                "tiktok_redirect_uri": settings.tiktok_redirect_uri
                if settings.tiktok_redirect_uri.startswith("https://") else "",
                "tiktok_mode": settings.tiktok_mode,
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

    youtube_flows: dict = {}

    @app.get("/auth/youtube")
    def auth_youtube(request: Request):
        from google_auth_oauthlib.flow import Flow

        from ..uploaders import youtube
        if not Path(settings.youtube_client_secrets).exists():
            raise HTTPException(400, f"fichier OAuth introuvable : {settings.youtube_client_secrets}")
        flow = Flow.from_client_secrets_file(
            settings.youtube_client_secrets, scopes=youtube.SCOPES,
            redirect_uri=str(request.url_for("auth_youtube_callback")),
        )
        url, state = flow.authorization_url(access_type="offline", prompt="consent")
        youtube_flows[state] = flow
        return RedirectResponse(url)

    @app.get("/auth/youtube/callback", name="auth_youtube_callback")
    def auth_youtube_callback(request: Request, state: str = "", error: str = ""):
        from ..uploaders import youtube
        flow = youtube_flows.pop(state, None)
        if error or flow is None:
            raise HTTPException(400, error or "state OAuth invalide, recommence la connexion")
        if request.url.scheme == "http" and request.url.hostname in {"localhost", "127.0.0.1"}:
            os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
        flow.fetch_token(authorization_response=str(request.url))
        youtube.save_credentials(flow.credentials)
        return RedirectResponse("/#/comptes")

    # ---------- mode automatique ----------
    from .. import automation

    def auto_submit(url: str, options: dict, on_done) -> None:
        job = Job(id=uuid.uuid4().hex[:10], url=url, kind="create", message="Automatique : en file d'attente")
        jobs[job.id] = job
        heavy.submit(run_create, job, JobIn(url=url, **options), on_done)

    def auto_publish(project_dir: Path, index: int, target: str) -> dict:
        from ..pipeline import publish_one
        return publish_one(project_dir, index, settings, target, tiktok_mode=settings.tiktok_mode)

    scheduler = automation.Scheduler(auto_submit, auto_publish, out_root)
    app.state.scheduler = scheduler
    if os.environ.get("OPENCLIP_SCHEDULER", "1") != "0":
        scheduler.start()

    @app.get("/api/automation")
    def automation_get():
        from ..uploaders import tiktok, youtube
        cfg = automation.load_settings()
        queue = sorted(automation.load_queue(), key=lambda q: (q["status"] != "pending", -q["score"]))
        return {**cfg, "queue": queue[:100], "status": scheduler.status,
                "connected": {"tiktok": tiktok.is_connected(), "youtube": youtube.is_connected()}}

    @app.put("/api/automation")
    async def automation_put(request: Request):
        changes = await request.json()
        return automation.save_settings(changes)

    @app.post("/api/automation/channels")
    async def automation_add_channel(request: Request):
        url = (await request.json()).get("url", "").strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "colle le lien de la chaîne YouTube")
        try:
            return automation.add_channel(url)
        except Exception as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/automation/channels/{channel_id}")
    def automation_remove_channel(channel_id: str):
        automation.remove_channel(channel_id)
        return {"ok": True}

    @app.post("/api/automation/check")
    def automation_check():
        return {"new_videos": scheduler.check_channels() + scheduler.check_searches()}

    @app.post("/api/automation/searches")
    async def automation_add_search(request: Request):
        try:
            return automation.add_search((await request.json()).get("query", ""))
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/automation/searches")
    def automation_remove_search(query: str):
        automation.remove_search(query)
        return {"ok": True}

    @app.get("/api/automation/preview")
    def automation_preview(query: str):
        """Aperçu des vidéos tendance qu'une recherche trouverait maintenant."""
        cfg = automation.load_settings()
        try:
            videos = automation.search_videos(query, cfg["search_period"])
        except Exception as exc:
            raise HTTPException(400, f"recherche impossible : {exc}")
        return automation.pick_trending(videos, set(), int(cfg["search_min_views"]),
                                        float(cfg["max_video_minutes"]), 10)

    @app.post("/api/automation/publish-next")
    def automation_publish_next():
        res = scheduler.publish_next()
        if not res:
            raise HTTPException(400, "aucun clip en attente")
        return res

    @app.post("/api/automation/queue")
    async def automation_enqueue(request: Request):
        body = await request.json()
        d = project_dir(body.get("project", ""))
        return {"added": automation.enqueue_project(d, float(body.get("min_score", 0)))}

    @app.delete("/api/automation/queue/{project}/{clip}")
    def automation_dequeue(project: str, clip: int):
        automation.remove_from_queue(project, clip)
        return {"ok": True}

    # ---------- applications TikTok / YouTube (publication) ----------
    @app.post("/api/settings/tiktok")
    def save_tiktok_settings(body: TikTokSettingsIn):
        from ..config import save_credentials
        if body.redirect_uri and not body.redirect_uri.startswith("https://"):
            raise HTTPException(400, "l'adresse de retour doit commencer par https://")
        if body.mode and body.mode not in {"draft", "direct"}:
            raise HTTPException(400, "mode inconnu")
        save_credentials(tiktok_client_key=(body.client_key or "").strip() or None,
                         tiktok_client_secret=(body.client_secret or "").strip() or None,
                         tiktok_redirect_uri=(body.redirect_uri or "").strip() or None,
                         tiktok_mode=body.mode)
        settings.reload_credentials()
        return {"ok": True}

    @app.post("/api/settings/youtube-client")
    async def save_youtube_client(request: Request):
        from ..config import YOUTUBE_CLIENT_FILE, ensure_dirs
        try:
            data = json.loads(await request.body())
        except ValueError:
            raise HTTPException(400, "ce n'est pas un fichier JSON")
        if "installed" not in data:
            kind = "Application Web" if "web" in data else "inconnu"
            raise HTTPException(400, f"type de client « {kind} » : crée un identifiant OAuth de type "
                                     "« Application de bureau » dans Google Cloud")
        if not data["installed"].get("client_id"):
            raise HTTPException(400, "client_id manquant dans le fichier")
        ensure_dirs()
        YOUTUBE_CLIENT_FILE.write_text(json.dumps(data))
        YOUTUBE_CLIENT_FILE.chmod(0o600)
        settings.reload_credentials()
        return {"ok": True}

    @app.post("/api/connect/{platform}")
    def connect_start(platform: str):
        from .. import oauth_remote
        if platform not in {"tiktok", "youtube"}:
            raise HTTPException(404, "plateforme inconnue")
        try:
            return oauth_remote.start(platform, settings)
        except Exception as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/connect/{platform}")
    def connect_poll(platform: str):
        from .. import oauth_remote
        return oauth_remote.poll(platform, settings)

    @app.delete("/api/connect/{platform}")
    def disconnect(platform: str):
        from ..uploaders import tiktok, youtube
        mod = {"tiktok": tiktok, "youtube": youtube}.get(platform)
        if not mod:
            raise HTTPException(404, "plateforme inconnue")
        mod.TOKEN_FILE.unlink(missing_ok=True)
        return {"ok": True}

    # ---------- connexion YouTube via navigateur distant ----------
    @app.get("/api/browser")
    def browser_status():
        from ..browser_login import browser
        return browser.status()

    @app.post("/api/browser/start")
    def browser_start():
        from ..browser_login import browser
        try:
            return {"token": browser.start(), **browser.status()}
        except RuntimeError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/browser/finish")
    def browser_finish():
        from ..browser_login import browser
        try:
            return browser.finish()
        except Exception as exc:
            raise HTTPException(400, f"cookies non récupérés : {exc}")

    @app.post("/api/browser/stop")
    def browser_stop():
        from ..browser_login import browser
        browser.stop()
        return browser.status()

    @app.post("/api/browser/refresh")
    def browser_refresh():
        from ..browser_login import browser
        try:
            return browser.refresh()
        except Exception as exc:
            raise HTTPException(400, str(exc))

    @app.websocket("/vnc")
    async def vnc_bridge(ws: WebSocket, token: str = ""):
        """Passerelle WebSocket <-> VNC local. Protégée par le jeton donné à /api/browser/start."""
        import asyncio

        from ..browser_login import VNC_PORT, browser
        if not browser.check_token(token):
            await ws.close(code=1008)
            return
        wanted = ws.scope.get("subprotocols") or []
        await ws.accept(subprotocol="binary" if "binary" in wanted else None)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
        except OSError:
            await ws.close(code=1011)
            return

        async def client_to_vnc():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                data = msg.get("bytes") or (msg.get("text") or "").encode()
                writer.write(data)
                await writer.drain()

        async def vnc_to_client():
            while data := await reader.read(65536):
                await ws.send_bytes(data)

        async def quiet(coro):
            try:
                await coro
            except Exception:  # déconnexion d'un côté ou de l'autre : on ferme tout
                pass

        tasks = [asyncio.create_task(quiet(client_to_vnc())), asyncio.create_task(quiet(vnc_to_client()))]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await ws.close()
        except Exception:  # client déjà parti
            pass

    if Path("/usr/share/novnc/core/rfb.js").exists():
        app.mount("/novnc", StaticFiles(directory="/usr/share/novnc"), name="novnc")

    # ---------- pages publiques (exigées par TikTok / Google pour valider une application) ----------
    brand = os.environ.get("OPENCLIP_BRAND", "OpenClip")
    contact = os.environ.get("OPENCLIP_CONTACT", "")

    def public_page(title: str, body: str) -> HTMLResponse:
        return HTMLResponse(f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · {brand}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;
color:#1d1d1f;background:#fff}}h1{{font-size:28px}}h2{{font-size:19px;margin-top:28px}}
@media (prefers-color-scheme:dark){{body{{background:#0d0e12;color:#f3f1ec}}}}</style></head>
<body><h1>{title}</h1>{body}<p style="margin-top:40px;opacity:.6">{brand}</p></body></html>""")

    @app.get("/legal/terms", response_class=HTMLResponse)
    def legal_terms():
        return public_page("Conditions d'utilisation", f"""
<p>{brand} est un outil privé de montage vidéo : il crée de courts extraits verticaux à partir de vidéos
et peut, à la demande de son utilisateur, les publier sur les comptes TikTok et YouTube que celui-ci
a lui-même connectés.</p>
<h2>Utilisation</h2><p>L'utilisateur s'engage à ne publier que des contenus dont il détient les droits
ou pour lesquels il a obtenu l'autorisation, et à respecter les conditions de TikTok et de YouTube.</p>
<h2>Responsabilité</h2><p>Le service est fourni tel quel. L'utilisateur reste seul responsable des contenus
qu'il publie.</p>{f"<h2>Contact</h2><p>{contact}</p>" if contact else ""}""")

    @app.get("/legal/privacy", response_class=HTMLResponse)
    def legal_privacy():
        return public_page("Politique de confidentialité", f"""
<p>{brand} est utilisé uniquement par son propriétaire, sur son propre serveur.</p>
<h2>Données traitées</h2><p>Lors de la connexion d'un compte TikTok ou YouTube, {brand} reçoit un jeton
d'accès et le nom public du compte. Ces jetons sont stockés sur le serveur dans des fichiers lisibles uniquement par le service, ne sont
jamais partagés, et servent uniquement à publier les vidéos choisies par l'utilisateur.</p>
<h2>Suppression</h2><p>Le bouton « Déconnecter » supprime immédiatement le jeton. L'accès peut aussi être
révoqué à tout moment depuis les paramètres TikTok ou Google du compte.</p>
<h2>Partage</h2><p>Aucune donnée n'est vendue ni transmise à des tiers.</p>
{f"<h2>Contact</h2><p>{contact}</p>" if contact else ""}""")

    @app.get("/callback/{platform}", response_class=HTMLResponse)
    def oauth_landing(platform: str):
        return public_page("Autorisation reçue",
                           "<p>Tu peux revenir dans OpenClip : la connexion se termine automatiquement.</p>")

    app.mount("/media", StaticFiles(directory=out_root), name="media")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    return app


def serve(host: str, port: int, out_root: Path) -> None:
    import uvicorn

    if host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("OPENCLIP_PASSWORD"):
        raise SystemExit(
            "Refus de démarrer : l'interface serait accessible à tout Internet sans mot de passe.\n"
            "Ajoute OPENCLIP_PASSWORD=... dans .env (et OPENCLIP_USER si tu veux un autre identifiant)."
        )
    print(f"OpenClip → http://{host}:{port}")
    # proxy_headers : garde le https d'origine derrière nginx (utile pour les redirections OAuth)
    uvicorn.run(create_app(out_root), host=host, port=port, log_level="warning",
                proxy_headers=True, forwarded_allow_ips="127.0.0.1")
