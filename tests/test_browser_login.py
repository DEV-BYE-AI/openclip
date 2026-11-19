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
