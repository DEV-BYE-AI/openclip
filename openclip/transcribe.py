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
    progress=None,
) -> list[Segment]:
    """Transcrit l'audio. `progress(fait_s, total_s)` est appelé au fil de l'eau."""
    if cache.exists():
        return load(cache)

    import os

    from faster_whisper import WhisperModel

    compute = "float16" if device == "cuda" else "int8"
    threads = int(os.environ.get("WHISPER_THREADS", "0")) or (os.cpu_count() or 2)
    model = WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=threads)
    audio = load_audio(media)
    total = len(audio) / 16000
    # Réglages rapides pour un CPU : décodage glouton (beam_size=1, ~2-3x plus rapide que 5)
    # et pas de contexte d'une phrase à l'autre (évite aussi les boucles de répétition).
    beam = int(os.environ.get("WHISPER_BEAM", "1" if device == "cpu" else "5"))
    raw, _info = model.transcribe(
        audio, word_timestamps=True, vad_filter=True, language=language,
        beam_size=beam, condition_on_previous_text=False,
    )
    segments = []
    for s in raw:  # générateur : la transcription avance segment par segment
        segments.append(Segment(
            start=s.start,
            end=s.end,
            text=s.text.strip(),
            words=[Word(w.start, w.end, w.word.strip()) for w in (s.words or []) if w.word.strip()],
        ))
        if progress:
            progress(min(s.end, total), total)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps([asdict(s) for s in segments], ensure_ascii=False))
    return segments


def load_audio(media: Path, sr: int = 16000):
    """Décode l'audio avec ffmpeg en mono 16 kHz (format attendu par Whisper).

    On n'utilise pas le décodeur de faster-whisper (PyAV) : ses versions récentes ont
    changé d'API et font planter la transcription.
    """
    import subprocess

    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(media), "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "f32le", "-"], capture_output=True, check=True,
    ).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    if audio.size == 0:
        raise RuntimeError("aucune piste audio dans la vidéo")
    return audio


def load(path: Path) -> list[Segment]:
    data = json.loads(path.read_text())
    return [
        Segment(d["start"], d["end"], d["text"], [Word(**w) for w in d["words"]]) for d in data
    ]


def words_between(segments: list[Segment], start: float, end: float) -> list[Word]:
    return [w for s in segments for w in s.words if w.end > start and w.start < end]
