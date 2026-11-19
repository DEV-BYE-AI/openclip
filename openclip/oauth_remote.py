"""Autoriser OpenClip à publier sur TikTok / YouTube, sans nom de domaine ni https.

La page d'autorisation officielle s'ouvre dans le navigateur qui tourne sur le serveur
(le même que pour « Se connecter à YouTube »), affiché dans l'interface. Après « J'autorise »,
TikTok / Google redirigent vers l'adresse de retour déclarée : peu importe que cette page
existe, OpenClip lit son adresse dans l'onglet (port de débogage local de Chrome), en extrait
le code d'autorisation et l'échange contre un jeton.

- YouTube : client OAuth Google de type « Application de bureau » → retour sur
  http://127.0.0.1:<port>/ (autorisé par Google pour ce type de client).
- TikTok : n'importe quelle adresse https déclarée dans l'application TikTok.
"""
from __future__ import annotations

import secrets
import threading
import time
import urllib.parse

from .browser_login import browser
from .config import Settings

YOUTUBE_REDIRECT = "http://127.0.0.1:8799/"


class PendingAuth:
    def __init__(self, platform: str, url: str, redirect: str, state: str, extra=None):
        self.platform = platform
        self.url = url
        self.redirect = redirect
        self.state = state
        self.extra = extra
        self.created = time.time()
        self.done = False
        self.error: str | None = None
        self.account: str | None = None


_pending: dict[str, PendingAuth] = {}
_lock = threading.Lock()


def start(platform: str, settings: Settings, direct: bool = False, account: str = "main") -> dict:
    """Prépare l'autorisation. `direct` : l'utilisateur ouvre la page dans son propre navigateur
    (possible quand l'adresse de retour TikTok est OpenClip lui-même, en https)."""
    state = secrets.token_urlsafe(16)
    if platform == "tiktok":
        from .uploaders import tiktok

        if not settings.tiktok_redirect_uri.startswith("https://"):
            raise RuntimeError("indique d'abord l'adresse de retour https déclarée dans ton app TikTok")
        url = tiktok.authorize_url(settings, state, settings.tiktok_redirect_uri)
        pending = PendingAuth("tiktok", url, settings.tiktok_redirect_uri, state, extra=account)
    elif platform == "youtube":
        from google_auth_oauthlib.flow import Flow

        from .uploaders import youtube

        flow = Flow.from_client_secrets_file(settings.youtube_client_secrets, scopes=youtube.SCOPES,
                                             redirect_uri=YOUTUBE_REDIRECT)
        url, _ = flow.authorization_url(access_type="offline", prompt="consent", state=state,
                                        include_granted_scopes="true")
        pending = PendingAuth("youtube", url, YOUTUBE_REDIRECT, state, extra=flow)
    else:
        raise ValueError("plateforme inconnue")
    with _lock:
        _pending[platform] = pending
    if direct:
        return {"url": url}
    token = browser.start(url)
    return {"token": token}


def finish_direct(platform: str, state: str, code: str, error: str, settings: Settings) -> dict:
    """Appelé par la page de retour d'OpenClip (/callback/<plateforme>)."""
    with _lock:
        pending = _pending.get(platform)
    if not pending or not state or not secrets.compare_digest(state, pending.state):
        return {"done": False, "error": "lien d'autorisation expiré : recommence depuis OpenClip"}
    if error:
        pending.error = error
        return {"done": False, "error": error}
    try:
        pending.account = _finish(pending, code, settings)
        pending.done = True
    except Exception as exc:
        pending.error = str(exc)
        return {"done": False, "error": str(exc)}
    return {"done": True, "account": pending.account}


def poll(platform: str, settings: Settings) -> dict:
    """Regarde si l'onglet est arrivé sur l'adresse de retour ; si oui, termine la connexion."""
    with _lock:
        pending = _pending.get(platform)
    if not pending:
        return {"pending": False}
    if pending.done or pending.error:
        return {"pending": False, "done": pending.done, "error": pending.error, "account": pending.account}

    target = urllib.parse.urlparse(pending.redirect)
    for url in browser.page_urls():
        u = urllib.parse.urlparse(url)
        if (u.scheme, u.netloc, u.path.rstrip("/")) != (target.scheme, target.netloc, target.path.rstrip("/")):
            continue
        q = urllib.parse.parse_qs(u.query)
        if q.get("state", [""])[0] != pending.state:
            continue
        if "error" in q:
            pending.error = q.get("error_description", q["error"])[0]
            break
        code = q.get("code", [""])[0]
        if not code:
            continue
        try:
            pending.account = _finish(pending, code, settings)
            pending.done = True
        except Exception as exc:  # code expiré, mauvais secret…
            pending.error = str(exc)
        break
    if pending.done or pending.error:
        browser.stop()
    return {"pending": not (pending.done or pending.error), "done": pending.done,
            "error": pending.error, "account": pending.account}


def _finish(pending: PendingAuth, code: str, settings: Settings) -> str:
    if pending.platform == "tiktok":
        from .uploaders import tiktok

        account = pending.extra or "main"
        tiktok.exchange_code(settings, code, pending.redirect, account)
        try:
            info = tiktok.creator_info(settings, account)
            return "@" + info.get("creator_username", "")
        except Exception:
            return ""
    from .uploaders import youtube

    flow = pending.extra
    flow.fetch_token(code=code)
    youtube.save_credentials(flow.credentials)
    return youtube.channel_name()
