"""Transcription locale avec faster-whisper (timestamps au mot près)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: list[Word]


def transcribe(
    media: Path,
    cache: Path,
    model_name: str = "small",
    device: str = "cpu",
    language: str | None = None,
) -> list[Segment]:
    if cache.exists():
        return load(cache)

    from faster_whisper import WhisperModel

    compute = "float16" if device == "cuda" else "int8"
    model = WhisperModel(model_name, device=device, compute_type=compute)
    raw, _info = model.transcribe(
        str(media), word_timestamps=True, vad_filter=True, language=language
    )
    segments = [
        Segment(
            start=s.start,
            end=s.end,
            text=s.text.strip(),
            words=[Word(w.start, w.end, w.word.strip()) for w in (s.words or []) if w.word.strip()],
        )
        for s in raw
    ]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([asdict(s) for s in segments], ensure_ascii=False))
    return segments


def load(path: Path) -> list[Segment]:
    data = json.loads(path.read_text())
    return [
        Segment(d["start"], d["end"], d["text"], [Word(**w) for w in d["words"]]) for d in data
    ]


def words_between(segments: list[Segment], start: float, end: float) -> list[Word]:
    return [w for s in segments for w in s.words if w.end > start and w.start < end]
