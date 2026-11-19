"""Publication YouTube Shorts via l'API YouTube Data v3."""
from __future__ import annotations

from pathlib import Path

from ..config import TOKENS_DIR, Settings, ensure_dirs

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]
TOKEN_FILE = TOKENS_DIR / "youtube.json"


def _credentials(settings: Settings, interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not interactive:
            raise RuntimeError("pas connecté à YouTube : lance `openclip auth youtube`")
        if not Path(settings.youtube_client_secrets).exists():
            raise RuntimeError(f"fichier OAuth introuvable : {settings.youtube_client_secrets}")
        flow = InstalledAppFlow.from_client_secrets_file(settings.youtube_client_secrets, SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
    ensure_dirs()
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)
    return creds


def save_credentials(creds) -> None:
    ensure_dirs()
    TOKEN_FILE.write_text(creds.to_json())
    TOKEN_FILE.chmod(0o600)


def login(settings: Settings) -> None:
    _credentials(settings, interactive=True)


def upload(settings: Settings, video: Path, title: str, description: str,
           tags: list[str], privacy: str = "public") -> dict:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=_credentials(settings))
    if "#shorts" not in (title + description).lower():
        description = f"{description}\n\n#Shorts".strip()
    body = {
        "snippet": {"title": title[:100], "description": description[:5000],
                    "tags": tags[:15], "categoryId": "22"},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    request = youtube.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=8 << 20),
    )
    from googleapiclient.errors import HttpError

    response = None
    try:
        while response is None:
            _status, response = request.next_chunk()
    except HttpError as exc:
        raise RuntimeError(friendly_error(exc)) from exc
    return {"video_id": response["id"], "url": f"https://youtube.com/shorts/{response['id']}"}


def is_connected() -> bool:
    return TOKEN_FILE.exists()


def channel_name() -> str:
    """Nom de la chaîne connectée (best effort)."""
    try:
        from googleapiclient.discovery import build

        from ..config import Settings
        yt = build("youtube", "v3", credentials=_credentials(Settings()))
        items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
        return items[0]["snippet"]["title"] if items else ""
    except Exception:
        return ""


def friendly_error(exc) -> str:
    """Traduit les erreurs courantes de l'API YouTube."""
    text = str(exc)
    if "uploadLimitExceeded" in text:
        return ("YouTube : limite d'envois atteinte pour ta chaîne aujourd'hui. Vérifie ta chaîne par "
                "téléphone sur youtube.com/verify (limite plus haute) et réessaie dans 24 h.")
    if "quotaExceeded" in text:
        return ("YouTube : quota quotidien de ton application Google épuisé (environ 6 envois par jour "
                "par défaut). Réessaie demain, ou demande plus de quota dans Google Cloud.")
    if "forbidden" in text.lower() or "insufficientPermissions" in text:
        return "YouTube : autorisation refusée. Reconnecte YouTube dans Comptes."
    return f"YouTube : {text[:300]}"
