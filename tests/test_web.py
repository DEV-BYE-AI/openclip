import json
from pathlib import Path

from fastapi.testclient import TestClient

from openclip.web.app import create_app


def make_project(root: Path) -> None:
    d = root / "abc123"
    d.mkdir(parents=True)
    (d / "clip_01.mp4").write_bytes(b"fake")
    manifest = {
        "source": {"video_id": "abc123", "title": "Ma vidéo", "url": "https://youtu.be/abc123",
                   "uploader": "Moi", "path": "/x.mp4", "transcript": "/x.json", "duration": 600},
        "settings": {}, "created_at": 1,
        "clips": [{"file": "clip_01.mp4", "start": 10, "end": 50, "title": "Titre", "hook": "Accroche",
                   "description": "", "hashtags": ["a"], "score": 88, "keywords": [], "uploads": {}}],
    }
    (d / "clips.json").write_text(json.dumps(manifest))


def test_projects_api(tmp_path: Path):
    make_project(tmp_path)
    client = TestClient(create_app(tmp_path))
    assert client.get("/").status_code == 200
    projects = client.get("/api/projects").json()
    assert projects[0]["id"] == "abc123" and projects[0]["best"] == 88
    p = client.get("/api/projects/abc123").json()
    assert "path" not in p["source"]  # les chemins locaux ne sortent pas
    r = client.patch("/api/projects/abc123/clips/1", json={"title": "Nouveau", "hashtags": ["x", "y"]})
    assert r.json()["clip"]["title"] == "Nouveau"
    assert client.get("/api/projects/../etc").status_code == 404
    assert "styles" in client.get("/api/meta").json()
    assert client.post("/api/jobs", json={"url": "pas un lien"}).status_code == 400
