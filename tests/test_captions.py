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
