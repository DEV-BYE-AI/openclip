"""Connexion à YouTube via un vrai navigateur qui tourne sur le serveur.

Google n'autorise aucune application à télécharger des vidéos via « Se connecter avec
Google », et bloque les connexions automatisées par e-mail / mot de passe. On lance donc
un vrai Chrome sur un écran virtuel (Xvfb), affiché dans l'interface web via VNC
(x11vnc, uniquement en local + passerelle WebSocket protégée par jeton). L'utilisateur se
connecte lui-même ; OpenClip exporte ensuite les cookies pour yt-dlp.

Le profil du navigateur est conservé : on peut ré-exporter des cookies frais plus tard
sans se reconnecter (tant que la session Google reste valide).
"""
from __future__ import annotations

import glob
import logging
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import HOME_DIR, ensure_dirs

log = logging.getLogger(__name__)

DISPLAY = os.environ.get("OPENCLIP_DISPLAY", ":77")
VNC_PORT = int(os.environ.get("OPENCLIP_VNC_PORT", "5977"))
WIDTH, HEIGHT = 540, 960  # portrait, texte lisible sur téléphone une fois l'image réduite
PROFILE_DIR = HOME_DIR / "browser-profile"
COOKIES_FILE = HOME_DIR / "youtube-cookies.txt"
LOGIN_URL = ("https://accounts.google.com/ServiceLogin?service=youtube&hl=fr"
             "&continue=https%3A%2F%2Fwww.youtube.com%2F")


def find_chrome() -> str | None:
    env = os.environ.get("OPENCLIP_CHROME")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    found = sorted(glob.glob("/opt/pw-browsers/chromium-*/chrome-linux*/chrome"))
    return found[-1] if found else None


def missing_tools() -> list[str]:
    missing = [t for t in ("Xvfb", "x11vnc") if not shutil.which(t)]
    if not find_chrome():
        missing.append("google-chrome")
    return missing


class LoginBrowser:
    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self.token: str | None = None

    # ------------------------------------------------------------------ état
    def running(self) -> bool:
        return all(p.poll() is None for p in self._procs.values()) and bool(self._procs)

    def status(self) -> dict:
        return {
            "running": self.running(),
            "missing": missing_tools(),
            "profile": PROFILE_DIR.exists(),
            "cookies": COOKIES_FILE.exists(),
            "cookies_updated_at": COOKIES_FILE.stat().st_mtime if COOKIES_FILE.exists() else None,
        }

    # ------------------------------------------------------------------ cycle de vie
    def start(self, url: str = LOGIN_URL) -> str:
        with self._lock:
            if self.running():
                return self.token or self._new_token()
            self._stop_locked()
            missing = missing_tools()
            if missing:
                raise RuntimeError("outils manquants sur le serveur : " + ", ".join(missing)
                                   + " (relance deploy/install.sh)")
            ensure_dirs()
            PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            # nettoie un verrou laissé par un arrêt brutal
            for lock in PROFILE_DIR.glob("Singleton*"):
                lock.unlink(missing_ok=True)

            env = {**os.environ, "DISPLAY": DISPLAY}
            quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            self._procs["xvfb"] = subprocess.Popen(
                ["Xvfb", DISPLAY, "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"], **quiet)
            self._wait_display(env)
            self._procs["vnc"] = subprocess.Popen(
                ["x11vnc", "-display", DISPLAY, "-localhost", "-rfbport", str(VNC_PORT), "-forever",
                 "-shared", "-nopw", "-quiet", "-noxdamage"], env=env, **quiet)
            args = [
                find_chrome(), f"--user-data-dir={PROFILE_DIR}", "--password-store=basic",
                "--no-first-run", "--no-default-browser-check", "--disable-dev-shm-usage",
                "--window-position=0,0", f"--window-size={WIDTH},{HEIGHT}", "--lang=fr",
                "--disable-features=Translate,TranslateUI,MediaRouter", "--force-device-scale-factor=1",
                "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
            ]
            if os.geteuid() == 0:
                args.append("--no-sandbox")
            self._procs["chrome"] = subprocess.Popen(args + [url], env=env, **quiet)
            self._wait_port(VNC_PORT)
            return self._new_token()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        # Chrome d'abord, avec SIGTERM : il écrit alors ses cookies sur le disque
        for name in ("chrome", "vnc", "xvfb"):
            proc = self._procs.pop(name, None)
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.token = None

    def _new_token(self) -> str:
        self.token = secrets.token_urlsafe(24)
        return self.token

    def check_token(self, token: str) -> bool:
        return bool(self.token) and secrets.compare_digest(token.encode(), self.token.encode())

    @staticmethod
    def _wait_display(env: dict, timeout: float = 10) -> None:
        sock = Path(f"/tmp/.X11-unix/X{DISPLAY.lstrip(':')}")
        deadline = time.time() + timeout
        while time.time() < deadline and not sock.exists():
            time.sleep(0.1)

    @staticmethod
    def _wait_port(port: int, timeout: float = 10) -> None:
        import socket

        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    return
            time.sleep(0.2)
        raise RuntimeError("le serveur VNC ne démarre pas")

    # ------------------------------------------------------------------ cookies
    def finish(self) -> dict:
        """Ferme le navigateur (pour qu'il enregistre la session) puis exporte les cookies."""
        self.stop()
        count = export_cookies()
        return {"cookies": count, **self.status()}

    def refresh(self, wait: float = 20) -> dict:
        """Rouvre YouTube avec le profil enregistré pour obtenir des cookies frais."""
        if not PROFILE_DIR.exists():
            raise RuntimeError("aucune session enregistrée : connecte-toi d'abord")
        self.start("https://www.youtube.com/")
        time.sleep(wait)
        return self.finish()


def export_cookies(dest: Path = COOKIES_FILE) -> int:
    """Lit les cookies du profil Chrome et écrit un cookies.txt (YouTube / Google uniquement)."""
    from http.cookiejar import MozillaCookieJar

    import yt_dlp
    from yt_dlp.cookies import load_cookies

    if not PROFILE_DIR.exists():
        raise RuntimeError("aucun profil de navigateur")
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        jar = load_cookies(None, ("chrome", str(PROFILE_DIR), "BASICTEXT", None), ydl)
    out = MozillaCookieJar(str(dest))
    keep = (".youtube.com", "youtube.com", ".google.com", "accounts.google.com", ".googlevideo.com")
    n = 0
    for c in jar:
        if c.domain.endswith(keep):
            # Chrome compte en microsecondes depuis 1601 : on convertit en secondes Unix
            if c.expires and c.expires > 10**11:
                c.expires = int(c.expires / 1_000_000 - 11_644_473_600)
            out.set_cookie(c)
            n += 1
    if not any(c.name in {"SID", "__Secure-3PSID", "LOGIN_INFO"} for c in out):
        raise RuntimeError("tu n'as pas l'air connecté à YouTube : termine la connexion puis réessaie")
    ensure_dirs()
    out.save(ignore_discard=True, ignore_expires=True)
    dest.chmod(0o600)
    return n


browser = LoginBrowser()
