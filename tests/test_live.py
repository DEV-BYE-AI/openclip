import subprocess
import time
from pathlib import Path

import pytest

import openclip.automation as auto
import openclip.config as config
from openclip import live


def make_hls(tmp: Path, seconds: int = 20) -> Path:
    d = tmp / "hls"
    d.mkdir()
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=300", "-t", str(seconds), "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", "-f", "hls", "-hls_time", "2",
                    "-hls_list_size", "0", str(d / "live.m3u8")], check=True)
    return d / "live.m3u8"


def test_live_url_and_name():
    assert live.live_url("https://www.youtube.com/@ishowspeed") == "https://www.youtube.com/@ishowspeed/live"
    assert live.live_url("https://www.twitch.tv/kaicenat/") == "https://www.twitch.tv/kaicenat"
    assert live.channel_name("https://www.twitch.tv/kaicenat") == "kaicenat"


def test_record_chunk_from_hls(tmp_path):
    m3u8 = make_hls(tmp_path)
    got = live.record({"url": str(m3u8)}, tmp_path / "out.mp4", 6)
    assert 5 <= got <= 7.5


def test_manager_records_and_submits(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    m3u8 = make_hls(tmp_path, 30)
    auto.save_settings({"enabled": True, "live_chunk_minutes": 15})
    auto.add_live("https://www.twitch.tv/streamer")
    monkeypatch.setattr(live, "probe_live", lambda url: {"url": str(m3u8), "title": "GTA 6 RP"})
    monkeypatch.setattr(live, "MIN_CHUNK_SECONDS", 5)
    submitted = []
    mgr = live.LiveManager(tmp_path / "out", lambda p, t, o, pid: submitted.append((p, t, o, pid)))
    mgr.tick()
    for _ in range(100):
        if submitted:
            break
        time.sleep(0.2)
    assert submitted, mgr.state
    path, title, opts, pid = submitted[0]
    assert path.exists() and title.startswith("Live streamer") and pid == "main"
    assert opts["num_clips"] == auto.load_settings()["clips_per_video"]
    st = mgr.status("main")["https://www.twitch.tv/streamer"]
    assert st["status"] == "processing" and st["chunks"] == 1


def test_offline_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    auto.save_settings({"enabled": True})
    auto.add_live("https://www.twitch.tv/off")
    monkeypatch.setattr(live, "probe_live", lambda url: None)
    mgr = live.LiveManager(tmp_path / "out", lambda *a: pytest.fail("ne doit rien envoyer"))
    mgr.tick()
    assert mgr.status("main")["https://www.twitch.tv/off"]["status"] == "offline"
    with pytest.raises(ValueError):
        auto.add_live("https://www.twitch.tv/off")
    auto.remove_live("https://www.twitch.tv/off")
    assert auto.load_settings()["lives"] == []


def test_live_to_clips_end_to_end(tmp_path, monkeypatch):
    """Live simulé -> morceau enregistré -> montage -> clips dans « À partager »."""
    from fastapi.testclient import TestClient

    import openclip.pipeline as pipeline
    from openclip.transcribe import Segment, Word
    from openclip.web.app import create_app

    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    monkeypatch.delenv("OPENCLIP_PASSWORD", raising=False)
    monkeypatch.setenv("OPENCLIP_SCHEDULER", "0")
    m3u8 = make_hls(tmp_path, 30)
    text = "Personne ne te dit ça mais voici le secret de Lucia dans Vice City ?".split()
    words = [Word(i * 0.4, i * 0.4 + 0.3, text[i % len(text)]) for i in range(60)]
    segs = [Segment(words[k].start, words[min(k + 5, 59)].end, " ".join(w.text for w in words[k:k + 6]),
                    words[k:k + 6]) for k in range(0, 60, 6)]
    monkeypatch.setattr(pipeline, "transcribe", lambda *a, **k: segs)
    monkeypatch.setattr(live, "probe_live", lambda url: {"url": str(m3u8), "title": "GTA 6 RP"})
    monkeypatch.setattr(live, "MIN_CHUNK_SECONDS", 5)

    app = create_app(tmp_path / "out")
    client = TestClient(app)
    client.put("/api/automation", json={"enabled": True, "live_chunk_minutes": 0.25, "clips_per_video": 2,
                                        "preset": "shorts", "crop_mode": "center"})
    # clips courts pour ce test (le preset « shorts » demande 30 s : on force via min/max)
    client.post("/api/automation/lives", json={"url": "https://www.twitch.tv/streamer"})
    monkeypatch.setattr(config, "PRESETS", {**config.PRESETS, "shorts": (6, 10)})
    import openclip.web.app as webapp
    monkeypatch.setattr(webapp, "PRESETS", {**webapp.PRESETS, "shorts": (6, 10)})
    client.post("/api/automation/lives/check")
    for _ in range(300):
        jobs = [j for j in client.get("/api/jobs").json() if j["url"].startswith("🔴")]
        if jobs and jobs[0]["stage"] in {"done", "error"}:
            break
        time.sleep(0.3)
    assert jobs and jobs[0]["stage"] == "done", jobs
    proj = client.get(f"/api/projects/{jobs[0]['project']}").json()
    assert proj["source"]["title"].startswith("Live streamer") and len(proj["clips"]) >= 1
    assert any(x["project"] == jobs[0]["project"] for x in client.get("/api/to-share").json())


def test_live_and_huge_videos_are_refused():
    from openclip.automation import pick_trending
    from openclip.download import NotDownloadable, check_downloadable
    with pytest.raises(NotDownloadable):
        check_downloadable({"live_status": "is_live"})
    with pytest.raises(NotDownloadable):
        check_downloadable({"duration": 10 * 3600})
    check_downloadable({"duration": 1800, "live_status": "not_live"})
    vids = [{"id": "a", "views": 9e6, "duration": None}, {"id": "b", "views": 9e6, "duration": 900, "live": True},
            {"id": "c", "views": 5e4, "duration": 900}]
    assert [v["id"] for v in pick_trending(vids, set(), 1000, 90, 5)] == ["c"]
    assert live.channel_name("https://www.youtube.com/live/oIeLbmYrarQ?si=x") == "live oIeLbmYrarQ"
