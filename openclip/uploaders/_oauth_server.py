"""Mini serveur local pour récupérer le code OAuth après autorisation dans le navigateur."""
from __future__ import annotations

import http.server
import urllib.parse


def wait_for_code(redirect_uri: str, expected_state: str, timeout: int = 300) -> str:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        url = input("Colle ici l'URL complète vers laquelle TikTok t'a redirigé : ").strip()
        return _extract(url, expected_state)

    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            try:
                result["code"] = _extract(self.path, expected_state)
                body = "Connexion réussie, tu peux fermer cet onglet."
            except ValueError as e:
                body = f"Erreur : {e}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    server.timeout = timeout
    while "code" not in result:
        server.handle_request()
    server.server_close()
    return result["code"]


def _extract(url: str, expected_state: str) -> str:
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    if "error" in q:
        raise ValueError(q.get("error_description", q["error"])[0])
    if q.get("state", [""])[0] != expected_state:
        raise ValueError("state OAuth invalide")
    if "code" not in q:
        raise ValueError("pas de code dans l'URL")
    return q["code"][0]
