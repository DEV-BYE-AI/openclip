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


def test_trending_search_picks_most_viewed(home, monkeypatch):
    out = home / "out"
    auto.save_settings({"enabled": True, "search_min_views": 1000, "max_new_videos_per_day": 3,
                        "max_video_minutes": 60, "slots": []})
    auto.add_search("GTA 6")
    vids = [
        {"id": "a" * 11, "title": "petite", "duration": 600, "views": 500, "channel": "x", "url": "u/a"},
        {"id": "b" * 11, "title": "top", "duration": 900, "views": 90000, "channel": "x", "url": "u/b"},
        {"id": "c" * 11, "title": "trop longue", "duration": 3 * 3600, "views": 99999, "channel": "x", "url": "u/c"},
        {"id": "d" * 11, "title": "short", "duration": 40, "views": 50000, "channel": "x", "url": "u/d"},
        {"id": "e" * 11, "title": "deuxième", "duration": 1200, "views": 20000, "channel": "x", "url": "u/e"},
    ]
    monkeypatch.setattr(auto, "search_videos", lambda q, period="week", limit=30: vids)
    submitted = []
    sch = auto.Scheduler(lambda url, o, cb: submitted.append(url), lambda *a: {}, out)
    assert sch.check_searches() == 2
    assert submitted == ["u/b", "u/e"]
    assert sch.check_searches() == 0  # déjà traitées
    assert sch._budget_left(auto.load_settings()) == 1  # 3 par jour - 2


def test_daily_budget_stops_processing(home, monkeypatch):
    auto.save_settings({"max_new_videos_per_day": 1, "search_min_views": 0})
    auto.add_search("GTA 6")
    vids = [{"id": c * 11, "title": c, "duration": 600, "views": 10, "channel": "", "url": "u/" + c} for c in "xyz"]
    monkeypatch.setattr(auto, "search_videos", lambda *a, **k: vids)
    submitted = []
    sch = auto.Scheduler(lambda url, o, cb: submitted.append(url), lambda *a: {}, home)
    sch.check_searches()
    assert len(submitted) == 1


def test_two_accounts_are_independent(home):
    out = home / "out"
    p2 = auto.add_profile("@inspecteurnews")
    assert p2["id"] == "inspecteurnews"
    assert [p["id"] for p in auto.list_profiles()] == ["main", "inspecteurnews"]

    auto.save_settings({"enabled": True, "slots": ["18:00"], "targets": ["tiktok"]})
    auto.enqueue_project(project(out, "gta", [90]), 0)
    with auto.use_profile("inspecteurnews"):
        auto.save_settings({"enabled": True, "slots": ["18:00"], "targets": ["tiktok"], "min_score": 70})
        auto.enqueue_project(project(out, "police", [80]), 0)
        assert [q["project"] for q in auto.load_queue()] == ["police"]
    assert [q["project"] for q in auto.load_queue()] == ["gta"]
    assert auto.load_settings()["min_score"] == 55  # réglages séparés

    published = []
    sch = auto.Scheduler(lambda *a: None,
                         lambda d, i, t: published.append((d.name, t, auto.current_profile())) or {"ok": 1}, out)
    sch.tick(datetime(2026, 11, 19, 18, 2))
    assert sorted(published) == [("gta", "tiktok", "main"), ("police", "tiktok", "inspecteurnews")]

    auto.delete_profile("inspecteurnews")
    assert [p["id"] for p in auto.list_profiles()] == ["main"]
    with pytest.raises(ValueError):
        auto.delete_profile("main")


def test_tiktok_token_per_account(home):
    from openclip.uploaders import tiktok
    assert tiktok.token_file("main").name == "tiktok.json"
    assert tiktok.token_file("inspecteurnews").name == "tiktok-inspecteurnews.json"
    assert not tiktok.is_connected("inspecteurnews")


def test_no_targets_keeps_queue(home):
    out = home / "out"
    auto.save_settings({"enabled": True, "targets": [], "slots": ["12:00"]})
    auto.enqueue_project(project(out, "p", [90]), 0)
    calls = []
    auto.Scheduler(lambda *a: None, lambda *a: calls.append(a), out).tick(datetime(2026, 1, 1, 12, 5))
    assert calls == [] and auto.load_queue()[0]["status"] == "pending"
