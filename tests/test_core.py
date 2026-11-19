import subprocess
from pathlib import Path

from openclip.highlights import Highlight, heuristic, remove_overlaps, snap
from openclip.render import build_filter, render_clip
from openclip.subtitles import build_ass, chunk_words, ts
from openclip.transcribe import Segment, Word
from openclip.uploaders.tiktok import chunk_plan


def fake_segments(n=40, seg_len=4.0):
    segs = []
    for i in range(n):
        s = i * seg_len
        words = [Word(s + j, s + j + 0.8, f"mot{i}_{j}") for j in range(4)]
        segs.append(Segment(s, s + seg_len, " ".join(w.text for w in words) + ("?" if i % 7 == 0 else "."), words))
    return segs


def test_snap_respects_duration_bounds():
    segs = fake_segments()
    for start, end in [(3, 20), (10, 130), (50.5, 95)]:
        h = snap(Highlight(start, end, "t"), segs, 30, 60)
        assert 30 <= h.duration <= 60.5, h


def test_heuristic_returns_non_overlapping_clips():
    clips = heuristic(fake_segments(80), 3, 30, 60)
    assert len(clips) == 3
    assert remove_overlaps(clips) == clips


def test_chunk_words_and_ass(tmp_path: Path):
    words = [Word(i * 0.5, i * 0.5 + 0.4, f"w{i}") for i in range(10)]
    groups = chunk_words(words)
    assert all(len(g) <= 3 for g in groups)
    out = build_ass(words, 0.0, 5.0, tmp_path / "x.ass", hook="Accroche {test}")
    text = out.read_text()
    assert "Accroche (test)" in text and text.count("Dialogue: 0") == 10
    assert ts(3661.5) == "1:01:01.50"


def test_tiktok_chunk_plan():
    assert chunk_plan(3_000_000) == (3_000_000, 1)
    size, count = chunk_plan(100 * 1024 * 1024)
    assert size >= 5 * 1024 * 1024 and count == 10


def test_crop_filter_bounds():
    vf = build_filter(1920, 1080, "face", 0.99, 1080, 1920, None)
    assert "crop=606:1080:1314:0" in vf


def test_render_end_to_end(tmp_path: Path):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "12", "-shortest", str(src)], check=True)
    words = [Word(2 + i * 0.4, 2.35 + i * 0.4, f"bonjour{i}") for i in range(10)]
    ass = build_ass(words, 2.0, 8.0, tmp_path / "c.ass", width=540, height=960, hook="Test")
    for mode in ("center", "blur"):
        out = render_clip(src, 2.0, 8.0, tmp_path / f"{mode}.mp4", mode=mode, ass=ass, out_w=540, out_h=960)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
                                "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout
        assert "540,960" in probe
