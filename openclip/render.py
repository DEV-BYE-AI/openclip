"""Découpe + recadrage 9:16 + sous-titres incrustés avec ffmpeg."""
from __future__ import annotations

import json
import logging
import statistics
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


def probe(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return int(s["width"]), int(s["height"])


def face_center_x(path: Path, start: float, end: float, samples: int = 12) -> float | None:
    """Position horizontale médiane (0-1) des visages détectés dans le clip, ou None."""
    try:
        import cv2
    except ImportError:
        log.info("opencv non installé (pip install 'openclip[face]') — recadrage centré.")
        return None

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(path))
    xs: list[float] = []
    try:
        for i in range(samples):
            t = start + (end - start) * (i + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.1, 6, minSize=(gray.shape[0] // 10,) * 2)
            if len(faces):
                x, _y, w, _h = max(faces, key=lambda f: f[2] * f[3])  # le plus grand visage
                xs.append((x + w / 2) / frame.shape[1])
    finally:
        cap.release()
    return statistics.median(xs) if xs else None


def _filter_path(p: Path) -> str:
    # échappement pour le filtre subtitles de ffmpeg
    return str(p.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def build_filter(src_w: int, src_h: int, mode: str, center_x: float | None,
                 out_w: int, out_h: int, ass: Path | None) -> str:
    if mode == "blur":
        vf = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},boxblur=25:5[bg2];"
            f"[fg]scale={out_w}:-2[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2"
        )
    else:
        crop_w = min(src_w, int(src_h * out_w / out_h) // 2 * 2)
        cx = center_x if center_x is not None else 0.5
        x = int(min(max(cx * src_w - crop_w / 2, 0), src_w - crop_w))
        vf = f"[0:v]crop={crop_w}:{src_h}:{x}:0,scale={out_w}:{out_h}"
    vf += ",setsar=1"
    if ass:
        vf += f",subtitles='{_filter_path(ass)}'"
    return vf + "[v]"


def render_clip(src: Path, start: float, end: float, out: Path, *, mode: str = "face",
                ass: Path | None = None, out_w: int = 1080, out_h: int = 1920) -> Path:
    src_w, src_h = probe(src)
    center = face_center_x(src, start, end) if mode == "face" else None
    vf = build_filter(src_w, src_h, mode, center, out_w, out_h, ass)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
        "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
        "-movflags", "+faststart", str(out),
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)
    return out
