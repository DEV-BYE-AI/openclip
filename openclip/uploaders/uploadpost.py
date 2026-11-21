"""Publication via Upload-Post (upload-post.com), un service qui possède déjà des applications
TikTok / YouTube / Instagram validées : on y connecte ses comptes une fois sur leur site, puis
OpenClip envoie les vidéos avec une simple clé API. Pas d'application développeur à créer,
et les vidéos TikTok peuvent être publiques tout de suite.

API : POST https://api.upload-post.com/api/upload, en-tête « Authorization: Apikey <clé> »,
formulaire multipart : user (nom du profil créé sur Upload-Post), platform[] (tiktok, youtube,
instagram…), title, video (fichier).
"""
from __future__ import annotations

from pathlib import Path

import requests

API = "https://api.upload-post.com/api"


class UploadPostError(RuntimeError):
    pass


def upload(api_key: str, profile: str, video: Path, platforms: list[str], title: str,
           description: str = "") -> dict:
    """Envoie la vidéo. Si le profil n'est pas reconnu, réessaie avec/sans « @ »
    (« gta6clipvideo » et « @gta6clipvideo » sont deux noms différents pour Upload-Post)."""
    try:
        return _upload(api_key, profile, video, platforms, title, description)
    except UploadPostError as exc:
        if "not associated" not in str(exc).lower() or not profile:
            raise
        other = profile[1:] if profile.startswith("@") else "@" + profile
        return _upload(api_key, other, video, platforms, title, description)


def _upload(api_key: str, profile: str, video: Path, platforms: list[str], title: str,
            description: str = "") -> dict:
    if not api_key or not profile:
        raise UploadPostError("Upload-Post : clé API ou nom de profil manquant (Comptes)")
    data = [("user", profile), ("title", title[:2200])]
    if description:
        data.append(("description", description[:5000]))
    data += [("platform[]", p) for p in platforms]
    with video.open("rb") as f:
        r = requests.post(f"{API}/upload", headers={"Authorization": f"Apikey {api_key}"},
                          data=data, files={"video": (video.name, f, "video/mp4")}, timeout=600)
    try:
        body = r.json()
    except ValueError:
        body = {"message": r.text[:300]}
    if r.status_code >= 400 or body.get("success") is False:
        msg = body.get("message") or body.get("error") or body
        if r.status_code == 401:
            msg = "clé API refusée"
        elif r.status_code == 429 or "limit" in str(msg).lower():
            msg = f"limite de ton forfait atteinte ({msg})"
        raise UploadPostError(f"Upload-Post : {msg}")
    return {"via": "upload-post", **{k: v for k, v in body.items() if k in {"results", "request_id", "message"}}}


def check_key(api_key: str) -> bool:
    """Vérifie la clé (best effort : l'API renvoie 401 si elle est invalide)."""
    try:
        r = requests.get(f"{API}/uploadposts/users", headers={"Authorization": f"Apikey {api_key}"}, timeout=20)
        return r.status_code != 401
    except requests.RequestException:
        return True
