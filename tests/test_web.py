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


def test_password_protection(tmp_path: Path, monkeypatch):
    import base64
    monkeypatch.setenv("OPENCLIP_PASSWORD", "s3cret")
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/projects").status_code == 401
    bad = base64.b64encode(b"admin:nope").decode()
    assert client.get("/api/projects", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"admin:s3cret").decode()
    assert client.get("/api/projects", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_refuses_public_bind_without_password(tmp_path: Path, monkeypatch):
    import pytest
    from openclip.web.app import serve
    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        serve("0.0.0.0", 8999, tmp_path)


def test_cookies_and_upload(tmp_path: Path, monkeypatch):
    import subprocess
    import time

    import openclip.config as config
    import openclip.pipeline as pipeline
    from openclip.transcribe import Segment, Word

    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    monkeypatch.delenv("YTDLP_COOKIES", raising=False)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "TOKENS_DIR", tmp_path / "home" / "tokens")
    client = TestClient(create_app(tmp_path / "out"))

    assert client.get("/api/cookies").json()["present"] is False
    assert client.post("/api/cookies", content=b"pas des cookies").status_code == 400
    good = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n"
    assert client.post("/api/cookies", content=good.encode()).json()["present"] is True
    assert (tmp_path / "home" / "youtube-cookies.txt").read_text().startswith("# Netscape")
    assert client.delete("/api/cookies").json()["present"] is False

    # vidéo envoyée -> clips, sans YouTube (transcription simulée)
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=300", "-t", "45", "-shortest", str(src)], check=True)
    words = [Word(i * 0.4, i * 0.4 + 0.3, w) for i, w in enumerate(("Personne ne te dit ça mais voici le secret "
             "pour gagner 1000 euros ? " * 12).split())]
    segs = [Segment(words[k].start, words[min(k + 9, len(words) - 1)].end,
                    " ".join(w.text for w in words[k:k + 10]), words[k:k + 10]) for k in range(0, len(words), 10)]
    monkeypatch.setattr(pipeline, "transcribe", lambda *a, **k: segs)

    assert client.post("/api/uploads", content=b"x", headers={"X-Filename": "a.exe"}).status_code == 400
    up = client.post("/api/uploads", content=src.read_bytes(), headers={"X-Filename": "Ma%20vid%C3%A9o.mp4"}).json()
    job = client.post("/api/jobs", json={"url": "upload:" + up["id"], "num_clips": 1, "min_seconds": 20,
                                         "max_seconds": 40, "crop_mode": "center"}).json()
    assert job["url"] == "📁 Ma vidéo"
    for _ in range(120):
        j = next(x for x in client.get("/api/jobs").json() if x["id"] == job["id"])
        if j["stage"] in {"done", "error"}:
            break
        time.sleep(0.5)
    assert j["stage"] == "done", j
    proj = client.get(f"/api/projects/{j['project']}").json()
    assert proj["source"]["title"] == "Ma vidéo" and len(proj["clips"]) == 1
    assert proj["clips"][0]["reason"]


def test_public_legal_pages_without_password(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OPENCLIP_PASSWORD", "s3cret")
    monkeypatch.setenv("OPENCLIP_BRAND", "Studio Test")
    client = TestClient(create_app(tmp_path))
    for path in ("/legal/terms", "/legal/privacy", "/callback/tiktok?code=x&state=y"):
        r = client.get(path)
        assert r.status_code == 200 and "Studio Test" in r.text
    assert client.get("/api/projects").status_code == 401  # le reste reste protégé
    assert client.get("/legal/../api/projects").status_code == 401


def test_more_clips_are_different_and_delete(tmp_path: Path, monkeypatch):
    import subprocess
    import time

    import openclip.config as config
    import openclip.pipeline as pipeline
    from openclip.transcribe import Segment, Word

    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    client = TestClient(create_app(tmp_path / "out"))
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15",
                    "-f", "lavfi", "-i", "sine", "-t", "60", "-shortest", str(src)], check=True)
    text = "Personne ne te dit ça mais voici le secret pour gagner 1000 euros ?".split()
    words = [Word(i * 0.5, i * 0.5 + 0.4, text[i % len(text)]) for i in range(118)]
    segs = [Segment(words[k].start, words[min(k + 5, len(words) - 1)].end,
                    " ".join(w.text for w in words[k:k + 6]), words[k:k + 6]) for k in range(0, len(words), 6)]
    monkeypatch.setattr(pipeline, "transcribe", lambda *a, **k: segs)

    def wait(job_id):
        for _ in range(200):
            j = next(x for x in client.get("/api/jobs").json() if x["id"] == job_id)
            if j["stage"] in {"done", "error"}:
                return j
            time.sleep(0.3)

    up = client.post("/api/uploads", content=src.read_bytes(), headers={"X-Filename": "v.mp4"}).json()
    opts = {"num_clips": 2, "min_seconds": 6, "max_seconds": 10, "crop_mode": "center", "jump_cuts": False}
    j = wait(client.post("/api/jobs", json={"url": "upload:" + up["id"], **opts}).json()["id"])
    assert j["stage"] == "done", j
    pid = j["project"]
    first = client.get(f"/api/projects/{pid}").json()["clips"]
    assert len(first) == 2

    j2 = wait(client.post(f"/api/projects/{pid}/more", json=opts).json()["id"])
    assert j2["stage"] == "done", j2
    clips = client.get(f"/api/projects/{pid}").json()["clips"]
    assert len(clips) == 4 and len({c["file"] for c in clips}) == 4
    spans = sorted((c["start"], c["end"]) for c in clips)
    assert all(b[0] >= a[1] - 1.6 for a, b in zip(spans, spans[1:]))  # d'autres moments

    assert client.delete(f"/api/projects/{pid}/clips/1").status_code == 200
    left = client.get(f"/api/projects/{pid}").json()["clips"]
    assert len(left) == 3 and not (tmp_path / "out" / pid / first[0]["file"]).exists()


def test_youtube_friendly_errors():
    from openclip.uploaders.youtube import friendly_error
    assert "youtube.com/verify" in friendly_error(Exception("reason: 'uploadLimitExceeded'"))
    assert "quota" in friendly_error(Exception("quotaExceeded"))


def test_delete_project(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    make_project(tmp_path)
    (tmp_path / "_sources").mkdir()
    (tmp_path / "_sources" / "abc123.transcript.json").write_text("[]")
    (tmp_path / "_sources" / "abc123.mp4").write_bytes(b"v")
    client = TestClient(create_app(tmp_path))
    assert client.delete("/api/projects/abc123").status_code == 200
    assert not (tmp_path / "abc123").exists()
    assert not (tmp_path / "_sources" / "abc123.transcript.json").exists()
    assert (tmp_path / "_sources" / "abc123.mp4").exists()  # vidéo gardée en cache


def test_upload_post_route(tmp_path: Path, monkeypatch):
    import openclip.config as config
    from openclip.pipeline import publish_one
    from openclip.uploaders import uploadpost

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "TOKENS_DIR", tmp_path / "home" / "tokens")
    monkeypatch.setattr(config, "CREDENTIALS_FILE", tmp_path / "home" / "credentials.json")
    make_project(tmp_path)
    sent = {}

    class Resp:
        status_code = 200
        def json(self):
            return {"success": True, "results": {"tiktok": {"success": True}}}

    def fake_post(url, headers, data, files, timeout):
        sent.update(url=url, auth=headers["Authorization"], data=data, name=files["video"][0])
        return Resp()

    monkeypatch.setattr(uploadpost.requests, "post", fake_post)
    config.save_credentials(upload_post_key="KEY123", upload_post_user="gta6", upload_post_platforms="tiktok")
    res = publish_one(tmp_path / "abc123", 1, config.Settings(), "tiktok")
    assert res["via"] == "upload-post" and sent["auth"] == "Apikey KEY123"
    assert ("user", "gta6") in sent["data"] and ("platform[]", "tiktok") in sent["data"]
    assert sent["url"].endswith("/api/upload") and sent["name"] == "clip_01.mp4"


def test_verification_file_is_public(tmp_path: Path, monkeypatch):
    import base64

    import openclip.config as config
    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    monkeypatch.setenv("OPENCLIP_PASSWORD", "pw")
    client = TestClient(create_app(tmp_path / "out"))
    auth = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    assert client.get("/tiktokAbC123.txt").status_code == 401  # n'existe pas : protégé comme le reste
    r = client.post("/api/verification-files", content=b"tiktok-developers-site-verification=XYZ",
                    headers={**auth, "X-Filename": "tiktokAbC123.txt"})
    assert r.status_code == 200
    pub = client.get("/tiktokAbC123.txt")  # sans mot de passe
    assert pub.status_code == 200 and pub.text == "tiktok-developers-site-verification=XYZ"
    bad = client.post("/api/verification-files", content=b"x", headers={**auth, "X-Filename": "../evil.txt"})
    assert bad.status_code == 400
    assert client.get("/api/projects").status_code == 401
    assert client.get("/", headers=auth).status_code == 200  # la page d'accueil n'est pas capturée


def test_to_share_list(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    make_project(tmp_path)
    client = TestClient(create_app(tmp_path))
    assert [x["clip"] for x in client.get("/api/to-share").json()] == [1]
    client.post("/api/projects/abc123/clips/1/shared", json={"shared": True})
    assert client.get("/api/to-share").json() == []
    client.post("/api/projects/abc123/clips/1/shared", json={"shared": False})
    assert len(client.get("/api/to-share").json()) == 1


def test_upload_post_retries_profile_with_at(tmp_path: Path, monkeypatch):
    from openclip.uploaders import uploadpost
    seen = []

    def fake(api_key, profile, *a, **k):
        seen.append(profile)
        if profile != "@gta6clipvideo":
            raise uploadpost.UploadPostError("Upload-Post : Username not associated with any profile")
        return {"via": "upload-post"}

    monkeypatch.setattr(uploadpost, "_upload", fake)
    assert uploadpost.upload("k", "gta6clipvideo", tmp_path / "v.mp4", ["tiktok"], "t")["via"] == "upload-post"
    assert seen == ["gta6clipvideo", "@gta6clipvideo"]
