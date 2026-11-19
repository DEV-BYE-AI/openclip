"""Publication YouTube Shorts via l'API YouTube Data v3."""
from __future__ import annotations

from pathlib import Path

from ..config import TOKENS_DIR, Settings, ensure_dirs

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
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
    response = None
    while response is None:
        _status, response = request.next_chunk()
    return {"video_id": response["id"], "url": f"https://youtube.com/shorts/{response['id']}"}
