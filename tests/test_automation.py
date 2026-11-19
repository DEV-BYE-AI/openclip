import json
from datetime import datetime
from pathlib import Path

import pytest

import openclip.automation as auto
import openclip.config as config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "TOKENS_DIR", tmp_path / "home" / "tokens")
    return tmp_path


def project(root: Path, name: str, scores):
    d = root / name
    d.mkdir(parents=True)
    clips = [{"file": f"clip_{i:02d}.mp4", "title": f"Clip {i}", "score": s, "uploads": {}}
             for i, s in enumerate(scores, 1)]
    (d / "clips.json").write_text(json.dumps({"source": {"title": name}, "clips": clips}))
    return d


def test_due_slot():
    now = datetime(2026, 11, 19, 18, 5)
    assert auto.due_slot(now, ["12:00", "18:00"], None) == "2026-11-19 18:00"
    assert auto.due_slot(now, ["12:00", "18:00"], "2026-11-19 18:00") is None  # déjà fait
    assert auto.due_slot(datetime(2026, 11, 19, 17, 0), ["18:00"], None) is None  # pas encore
    assert auto.due_slot(datetime(2026, 11, 19, 21, 0), ["18:00"], None) is None  # trop tard (> 2 h)


def test_settings_validation(home):
    s = auto.save_settings({"slots": ["21:00", "9:5", "25:00", "08:30"], "min_score": 70, "channels": ["x"]})
    assert s["slots"] == ["08:30", "21:00"] and s["min_score"] == 70 and s["channels"] == []


def test_queue_best_first(home):
    out = home / "out"
    auto.enqueue_project(project(out, "a", [40, 90, 60]), min_score=55)
    assert [q["clip"] for q in auto.load_queue()] == [2, 3]
    assert auto.enqueue_project(out / "a", 55) == 0  # pas de doublon
    assert auto.next_pending()["clip"] == 2


def test_scheduler_new_video_then_publish(home, monkeypatch):
    out = home / "out"
    auto.save_settings({"enabled": True, "slots": ["18:00"], "targets": ["tiktok", "youtube"]})
    data = auto.load_settings()
    data["channels"] = [{"channel_id": "UCx", "name": "Chaîne", "seen": ["old"]}]
    auto._write(auto._files()[0], data)
    monkeypatch.setattr(auto, "fetch_feed", lambda cid: {"name": "Chaîne", "videos": [
        {"id": "new1", "url": "https://www.youtube.com/watch?v=new1", "title": "t", "published": ""},
        {"id": "old", "url": "https://www.youtube.com/watch?v=old", "title": "t", "published": ""}]})

    submitted, published = [], []

    def submit(url, opts, on_done):
        submitted.append((url, opts))
        on_done(project(out, "p1", [80, 30]))

    def publish(d, i, target):
        published.append((d.name, i, target))
        if target == "youtube":
            raise RuntimeError("pas connecté")
        return {"publish_id": "x"}

    sch = auto.Scheduler(submit, publish, out)
    sch.tick(datetime(2026, 11, 19, 18, 1))
    assert [u for u, _ in submitted] == ["https://www.youtube.com/watch?v=new1"]
    assert published == [("p1", 1, "tiktok"), ("p1", 1, "youtube")]
    q = auto.load_queue()[0]
    assert q["status"] == "published" and "tiktok" in q["results"] and "youtube" in q["errors"]
    sch.tick(datetime(2026, 11, 19, 18, 30))  # même créneau : rien de plus
    assert len(published) == 2 and len(submitted) == 1


def test_disabled_does_nothing(home):
    calls = []
    auto.Scheduler(lambda *a: calls.append(a), lambda *a: calls.append(a), home).tick(datetime(2026, 1, 1, 12, 0))
    assert calls == []
