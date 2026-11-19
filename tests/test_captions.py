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
