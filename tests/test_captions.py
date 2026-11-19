import json
from pathlib import Path

from openclip.download import parse_json3


def test_parse_json3_word_timings(tmp_path: Path):
    data = {"events": [
        {"tStartMs": 0, "dDurationMs": 3000, "segs": [
            {"utf8": "personne"}, {"utf8": " ne", "tOffsetMs": 400}, {"utf8": " te", "tOffsetMs": 600},
            {"utf8": " dit", "tOffsetMs": 800}, {"utf8": " ça", "tOffsetMs": 1100}]},
        {"tStartMs": 1500, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 3000, "dDurationMs": 2000, "segs": [
            {"utf8": "le"}, {"utf8": " secret", "tOffsetMs": 300}, {"utf8": " c'est", "tOffsetMs": 900}]},
        {"tStartMs": 8000, "dDurationMs": 2000, "segs": [{"utf8": "phrase sans horodatage par mot"}]},
    ]}
    p = tmp_path / "x.json3"
    p.write_text(json.dumps(data))
    segs = parse_json3(p)
    words = [w for s in segs for w in s.words]
    assert [w.text for w in words[:5]] == ["personne", "ne", "te", "dit", "ça"]
    assert words[1].start == 0.4 and words[0].end <= words[1].start
    assert all(a.start <= b.start for a, b in zip(words, words[1:]))
    # la pause de 3 s avant la dernière ligne coupe une phrase
    assert segs[-1].text == "phrase sans horodatage par mot"
    assert abs(segs[-1].words[-1].end - 10.0) < 0.01


def test_overlapping_lines_keep_order(tmp_path: Path):
    # sous-titres traduits / manuels : lignes qui se chevauchent, sans horodatage par mot
    data = {"events": [
        {"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "un deux trois quatre"}]},
        {"tStartMs": 2000, "dDurationMs": 4000, "segs": [{"utf8": "cinq six sept huit"}]},
    ]}
    p = tmp_path / "o.json3"
    p.write_text(json.dumps(data))
    words = [w.text for s in parse_json3(p) for w in s.words]
    assert words == ["un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit"]


def test_wrong_language_captions_are_rejected(tmp_path: Path, monkeypatch):
    import openclip.pipeline as pipeline
    import openclip.transcribe as tr
    from openclip.config import Settings
    from openclip.download import SourceVideo

    src = SourceVideo("vid", "Ma vidéo", tmp_path / "v.mp4", 600, "u")
    cap = tmp_path / "vid.captions.en-orig.json3"
    monkeypatch.setattr(tr, "detect_spoken_language", lambda *a, **k: ("fr", 0.97))
    assert pipeline._captions_language_ok(cap, src, Settings(), lambda *a: None) is False
    monkeypatch.setattr(tr, "detect_spoken_language", lambda *a, **k: ("en", 0.97))
    assert pipeline._captions_language_ok(cap, src, Settings(), lambda *a: None) is True


def test_title_language():
    from openclip.analyzer import title_language
    assert title_language("GTA 6 TEST ⭐ J'AI FINI le JEU de ROCKSTAR c'est MONUMENTAL (SANS SPOILER)") == "fr"
    assert title_language("How I built this in 30 days and what you should know") == "en"


def test_legacy_youtube_transcript_is_rechecked(tmp_path: Path, monkeypatch):
    """Une transcription faite par une ancienne version à partir de sous-titres anglais erronés
    doit être jetée si la vidéo est en français."""
    import openclip.pipeline as pipeline
    import openclip.transcribe as tr
    from openclip.config import Settings
    from openclip.download import SourceVideo
    from openclip.transcribe import Segment, Word

    work = tmp_path / "out" / "_sources"
    work.mkdir(parents=True)
    (work / "vid.transcript.json").write_text("[]")
    (work / "vid.captions.en-orig.json3").write_text('{"events": []}')
    src = SourceVideo("vid", "J'ai fini le jeu", work / "vid.mp4", 600, "u")
    monkeypatch.setattr(pipeline, "safe_download", lambda url, d: src)
    monkeypatch.setattr(pipeline, "download_captions", lambda *a: None)
    monkeypatch.setattr(tr, "detect_spoken_language", lambda *a, **k: ("fr", 0.95))
    seen = {}

    def fake_transcribe(media, cache, *a, **k):
        seen["redone"] = not cache.exists()
        raise RuntimeError("stop")  # on s'arrête après la décision

    monkeypatch.setattr(pipeline, "transcribe", fake_transcribe)
    import pytest
    with pytest.raises(RuntimeError, match="stop"):
        pipeline.make_clips("https://youtu.be/vid", Settings(), tmp_path / "out")
    assert seen["redone"] is True  # l'ancienne transcription anglaise a été jetée
