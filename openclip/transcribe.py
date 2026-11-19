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

    model = _model(model_name, device)
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


_MODELS: dict = {}


def _model(model_name: str, device: str):
    import os

    from faster_whisper import WhisperModel

    key = (model_name, device)
    if key not in _MODELS:
        compute = "float16" if device == "cuda" else "int8"
        threads = int(os.environ.get("WHISPER_THREADS", "0")) or (os.cpu_count() or 2)
        _MODELS[key] = WhisperModel(model_name, device=device, compute_type=compute, cpu_threads=threads)
    return _MODELS[key]


def detect_spoken_language(media: Path, duration: float, model_name: str = "base",
                           device: str = "cpu") -> tuple[str, float]:
    """Langue réellement parlée : Whisper écoute 3 extraits de 30 s (début, milieu, fin).

    On cumule les probabilités : un passage de jeu vidéo en anglais au milieu d'une vidéo
    commentée en français ne suffit pas à fausser le résultat.
    """
    model = _model(model_name, device)
    marks = [0.2, 0.5, 0.8] if duration and duration > 120 else [0.0]
    totals: dict[str, float] = {}
    for frac in marks:
        start = max(0.0, min(duration * frac, duration - 30)) if duration else 0.0
        audio = load_audio(media, start=start, length=30)
        if audio.size < 16000 * 3:
            continue
        _, _, probs = model.detect_language(audio)
        for lang, prob in probs[:5]:
            totals[lang] = totals.get(lang, 0.0) + prob
    if not totals:
        return "", 0.0
    lang = max(totals, key=totals.get)
    return lang, totals[lang] / len(marks)


def load_audio(media: Path, sr: int = 16000, start: float | None = None, length: float | None = None):
    """Décode l'audio avec ffmpeg en mono 16 kHz (format attendu par Whisper).

    On n'utilise pas le décodeur de faster-whisper (PyAV) : ses versions récentes ont
    changé d'API et font planter la transcription.
    """
    import subprocess

    import numpy as np

    cut = (["-ss", f"{start:.2f}"] if start else []) + (["-t", f"{length:.2f}"] if length else [])
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", *cut, "-i", str(media), "-vn", "-ac", "1", "-ar", str(sr),
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
