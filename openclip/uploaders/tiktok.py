"""Publication TikTok via l'API officielle Content Posting (v2).

Docs : https://developers.tiktok.com/doc/content-posting-api-get-started
- mode "direct" : publie directement (scope video.publish). Tant que ton app n'est pas
  auditée par TikTok, les posts sont forcés en privé (SELF_ONLY).
- mode "draft"  : envoie la vidéo dans la boîte de réception TikTok (scope video.upload),
  tu termines la publication dans l'app (son, texte...).
"""
from __future__ import annotations

import json
import math
import secrets
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

from ..config import TOKENS_DIR, Settings, ensure_dirs
from ._oauth_server import wait_for_code

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
TOKEN_FILE = TOKENS_DIR / "tiktok.json"
SCOPES = "user.info.basic,video.publish,video.upload"
MIN_CHUNK = 5 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


class TikTokError(RuntimeError):
    pass


def login(settings: Settings) -> dict:
    if not settings.tiktok_client_key or not settings.tiktok_client_secret:
        raise TikTokError("TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET manquants dans .env")
    state = secrets.token_urlsafe(16)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_key": settings.tiktok_client_key,
        "scope": SCOPES,
        "response_type": "code",
        "redirect_uri": settings.tiktok_redirect_uri,
        "state": state,
    })
    print(f"Ouvre ce lien pour autoriser OpenClip sur ton compte TikTok :\n{url}\n")
    webbrowser.open(url)
    code = wait_for_code(settings.tiktok_redirect_uri, state)
    token = _token_request(settings, {
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.tiktok_redirect_uri,
    })
    _save(token)
    return token


def _token_request(settings: Settings, data: dict) -> dict:
    r = requests.post(
        f"{API}/oauth/token/",
        data={"client_key": settings.tiktok_client_key,
              "client_secret": settings.tiktok_client_secret, **data},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    body = r.json()
    if "access_token" not in body:
        raise TikTokError(f"échec OAuth TikTok : {body}")
    body["obtained_at"] = time.time()
    return body


def _save(token: dict) -> None:
    ensure_dirs()
    TOKEN_FILE.write_text(json.dumps(token))
    TOKEN_FILE.chmod(0o600)


def access_token(settings: Settings) -> str:
    if not TOKEN_FILE.exists():
        raise TikTokError("pas connecté à TikTok : lance `openclip auth tiktok`")
    token = json.loads(TOKEN_FILE.read_text())
    if time.time() > token["obtained_at"] + token.get("expires_in", 0) - 300:
        token = _token_request(settings, {"grant_type": "refresh_token",
                                          "refresh_token": token["refresh_token"]})
        _save(token)
    return token["access_token"]


def _post(path: str, token: str, payload: dict | None = None) -> dict:
    r = requests.post(
        f"{API}{path}", json=payload or {}, timeout=60,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
    )
    body = r.json()
    err = body.get("error", {})
    if r.status_code >= 400 or err.get("code") not in (None, "ok"):
        raise TikTokError(f"{path} : {err or body}")
    return body.get("data", {})


def creator_info(settings: Settings) -> dict:
    return _post("/post/publish/creator_info/query/", access_token(settings))


def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) selon les règles TikTok (5-64 Mo, dernier ≤128 Mo)."""
    if size <= CHUNK + MIN_CHUNK:
        return size, 1
    return CHUNK, size // CHUNK  # le reste est absorbé par le dernier morceau


def upload(settings: Settings, video: Path, caption: str, *, mode: str = "direct",
           privacy: str | None = None, wait: bool = True) -> dict:
    token = access_token(settings)
    size = video.stat().st_size
    chunk_size, count = chunk_plan(size)
    source = {"source": "FILE_UPLOAD", "video_size": size,
              "chunk_size": chunk_size, "total_chunk_count": count}

    if mode == "draft":
        data = _post("/post/publish/inbox/video/init/", token, {"source_info": source})
    else:
        info = _post("/post/publish/creator_info/query/", token)
        options = info.get("privacy_level_options", ["SELF_ONLY"])
        level = privacy if privacy in options else ("PUBLIC_TO_EVERYONE"
                if privacy is None and "PUBLIC_TO_EVERYONE" in options else options[0])
        post_info = {
            "title": caption[:2200],
            "privacy_level": level,
            "disable_comment": bool(info.get("comment_disabled", False)),
            "disable_duet": bool(info.get("duet_disabled", False)),
            "disable_stitch": bool(info.get("stitch_disabled", False)),
            "video_cover_timestamp_ms": 1000,
        }
        data = _post("/post/publish/video/init/", token,
                     {"post_info": post_info, "source_info": source})

    publish_id, upload_url = data["publish_id"], data["upload_url"]
    with video.open("rb") as f:
        for i in range(count):
            start = i * chunk_size
            end = size - 1 if i == count - 1 else start + chunk_size - 1
            f.seek(start)
            blob = f.read(end - start + 1)
            r = requests.put(upload_url, data=blob, timeout=300, headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(len(blob)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            })
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"échec upload morceau {i + 1}/{count} : {r.status_code} {r.text}")

    result = {"publish_id": publish_id, "status": "UPLOADED"}
    if wait:
        result = wait_status(token, publish_id)
    return result


def wait_status(token: str, publish_id: str, timeout: int = 600) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _post("/post/publish/status/fetch/", token, {"publish_id": publish_id})
        status = data.get("status", "")
        if status in {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"}:
            return {"publish_id": publish_id, **data}
        if status == "FAILED":
            raise TikTokError(f"publication échouée : {data.get('fail_reason')}")
        time.sleep(5)
    return {"publish_id": publish_id, "status": "TIMEOUT"}
