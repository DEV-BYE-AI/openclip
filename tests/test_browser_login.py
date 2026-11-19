from openclip.browser_login import missing_tools
from openclip.web.app import create_app
from fastapi.testclient import TestClient


def test_browser_status_and_ws_requires_token(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    client = TestClient(create_app(tmp_path))
    st = client.get("/api/browser").json()
    assert st["running"] is False and isinstance(st["missing"], list)
    assert missing_tools() == st["missing"]
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/vnc?token=faux") as ws:
            ws.receive_bytes()


def test_publication_settings(tmp_path, monkeypatch):
    import json

    import openclip.config as config
    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "TOKENS_DIR", tmp_path / "home" / "tokens")
    monkeypatch.setattr(config, "CREDENTIALS_FILE", tmp_path / "home" / "credentials.json")
    monkeypatch.setattr(config, "YOUTUBE_CLIENT_FILE", tmp_path / "home" / "yt.json")
    client = TestClient(create_app(tmp_path / "out"))

    assert client.post("/api/settings/tiktok", json={"redirect_uri": "http://x"}).status_code == 400
    r = client.post("/api/settings/tiktok", json={"client_key": "ck", "client_secret": "cs",
                                                  "redirect_uri": "https://exemple.fr/tt", "mode": "draft"})
    assert r.status_code == 200
    acc = client.get("/api/meta").json()["accounts"]
    assert acc["tiktok_configured"] and acc["tiktok_client_key"] == "ck" and acc["tiktok_mode"] == "draft"
    assert "cs" not in json.dumps(acc)  # le secret ne ressort jamais

    web = {"web": {"client_id": "x"}}
    assert client.post("/api/settings/youtube-client", content=json.dumps(web)).status_code == 400
    ok = {"installed": {"client_id": "id", "client_secret": "s", "auth_uri": "a", "token_uri": "t"}}
    assert client.post("/api/settings/youtube-client", content=json.dumps(ok)).status_code == 200
    assert client.get("/api/meta").json()["accounts"]["youtube_configured"] is True
