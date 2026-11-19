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

    try:
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except AttributeError:  # OpenCV 5 n'a plus ce détecteur : on recadre au centre
        log.warning("détecteur de visage indisponible (OpenCV %s) — recadrage centré", cv2.__version__)
        return None
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


def content_center_x(path: Path, start: float, end: float, crop_frac: float,
                     samples: int = 16) -> float | None:
    """Sans visage (jeu vidéo, paysage…) : où se trouve l'action dans l'image ?

    On mesure, colonne par colonne, le mouvement entre images et les détails (contours),
    en ignorant les bandes du haut et du bas (souvent des éléments d'interface), puis on
    choisit la fenêtre verticale 9:16 qui en contient le plus, avec une légère préférence
    pour le centre. Renvoie la position horizontale (0-1) du centre de cette fenêtre.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    cap = cv2.VideoCapture(str(path))
    prev, energy = None, None
    try:
        for i in range(samples):
            t = start + (end - start) * (i + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (320, max(1, int(320 * h / w))))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            band = gray[int(gray.shape[0] * 0.12): int(gray.shape[0] * 0.88)]
            edges = np.abs(cv2.Sobel(band, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(band, cv2.CV_32F, 0, 1))
            col = edges.mean(axis=0)
            if prev is not None and prev.shape == band.shape:
                col = col + 3 * np.abs(band - prev).mean(axis=0)  # le mouvement compte plus
            prev = band
            energy = col if energy is None else energy + col
    finally:
        cap.release()
    if energy is None:
        return None
    n = len(energy)
    win = max(1, int(n * crop_frac))
    if win >= n:
        return 0.5
    sums = np.convolve(energy, np.ones(win), mode="valid")  # énergie de chaque fenêtre possible
    centers = (np.arange(len(sums)) + win / 2) / n
    prior = 1 - 0.35 * np.abs(centers - 0.5) / 0.5  # léger avantage au centre
    score = sums / (sums.max() or 1) * prior
    best = float(centers[int(score.argmax())])
    # si l'action est presque uniforme, on reste au centre (plus naturel)
    return best if score.max() - score[len(score) // 2] > 0.08 else 0.5


def has_audio(path: Path) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True,
    ).stdout
    return bool(out.strip())


def keep_intervals(words, start: float, end: float, max_gap: float = 0.6,
                   pad: float = 0.12) -> list[tuple[float, float]]:
    """Intervalles à garder pour supprimer les blancs > max_gap (jump cuts)."""
    ws = sorted((w for w in words if w.end > start and w.start < end), key=lambda w: w.start)
    if not ws:
        return [(start, end)]
    intervals: list[tuple[float, float]] = []
    a, prev_end = start, max(start, ws[0].start)
    for w in ws:
        if w.start - prev_end > max_gap:
            intervals.append((a, min(prev_end + pad, end)))
            a = max(w.start - pad, start)
        prev_end = max(prev_end, w.end)
    intervals.append((a, end))
    return [(x, y) for x, y in intervals if y - x > 0.05]


def remap_words(words, intervals: list[tuple[float, float]]):
    """Recale les timestamps des mots sur la timeline après suppression des blancs."""
    from .transcribe import Word

    out, offset = [], 0.0
    for a, b in intervals:
        for w in words:
            if a <= w.start < b:
                out.append(Word(offset + w.start - a, offset + min(w.end, b) - a, w.text))
        offset += b - a
    return out


def _filter_path(p: Path) -> str:
    # échappement pour le filtre subtitles de ffmpeg
    return str(p.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def build_graph(src_w: int, src_h: int, mode: str, center_x: float | None, out_w: int,
                out_h: int, ass: Path | None, *, intervals: list[tuple[float, float]] | None = None,
                audio: bool = True, progress_bar: bool = True, duration: float = 0.0,
                loudnorm: bool = True, bar_color: str = "0xFFD400") -> str:
    """Construit le filter_complex. Les intervalles sont relatifs au début du clip."""
    chains: list[str] = []
    if intervals and len(intervals) > 1:
        ins = ""
        for i, (a, b) in enumerate(intervals):
            chains.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
            ins += f"[v{i}]"
            if audio:
                chains.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
                ins += f"[a{i}]"
        chains.append(f"{ins}concat=n={len(intervals)}:v=1:a={int(audio)}[vc]" + ("[ac]" if audio else ""))
    else:
        chains.append("[0:v]null[vc]")
        if audio:
            chains.append("[0:a]anull[ac]")

    if mode == "blur":
        chains.append(
            f"[vc]split=2[bg][fg];"
            # le flou est calculé en petite taille puis agrandi : ~5x plus rapide, rendu identique
            f"[bg]scale={out_w // 4}:{out_h // 4}:force_original_aspect_ratio=increase,"
            f"crop={out_w // 4}:{out_h // 4},boxblur=8:3,scale={out_w}:{out_h},eq=brightness=-0.08[bg2];"
            f"[fg]scale={out_w}:-2[fg2];"
            f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1[vf]"
        )
    else:
        crop_w = min(src_w, int(src_h * out_w / out_h) // 2 * 2)
        cx = center_x if center_x is not None else 0.5
        x = int(min(max(cx * src_w - crop_w / 2, 0), src_w - crop_w))
        chains.append(f"[vc]crop={crop_w}:{src_h}:{x}:0,scale={out_w}:{out_h},setsar=1[vf]")

    last = "vf"
    if ass:
        chains.append(f"[{last}]subtitles='{_filter_path(ass)}'[vs]")
        last = "vs"
    if progress_bar and duration > 0:
        h = max(8, out_h // 160)
        chains.append(f"color=c={bar_color}:s={out_w}x{h}:r=30[bar]")
        chains.append(f"[{last}][bar]overlay=x='-W+W*t/{duration:.3f}':y=H-h:shortest=1[vb]")
        last = "vb"
    chains.append(f"[{last}]null[v]")
    if audio:
        chains.append("[ac]" + ("loudnorm=I=-14:TP=-1.5:LRA=11," if loudnorm else "") + "aresample=44100[a]")
    return ";".join(chains)


def render_clip(src: Path, start: float, end: float, out: Path, *, mode: str = "face",
                ass: Path | None = None, out_w: int = 1080, out_h: int = 1920,
                intervals: list[tuple[float, float]] | None = None,
                progress_bar: bool = True, loudnorm: bool = True, on_progress=None) -> Path:
    """Rend le clip. `intervals` (absolus, issus de keep_intervals) active les jump cuts."""
    src_w, src_h = probe(src)
    audio = has_audio(src)
    center = None
    if mode == "face":
        try:
            center = face_center_x(src, start, end)
            if center is None:  # pas de visage : on suit l'action
                crop_frac = min(1.0, src_h * out_w / out_h / src_w)
                center = content_center_x(src, start, end, crop_frac)
        except Exception as exc:  # la détection ne doit jamais empêcher le montage
            log.warning("analyse du cadrage impossible (%s) — recadrage centré", exc)
    rel = [(a - start, b - start) for a, b in intervals] if intervals else None
    duration = sum(b - a for a, b in rel) if rel else end - start
    graph = build_graph(src_w, src_h, mode, center, out_w, out_h, ass, intervals=rel,
                        audio=audio, progress_bar=progress_bar, duration=duration,
                        loudnorm=loudnorm)
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src),
        "-filter_complex", graph, "-map", "[v]",
    ]
    if audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
    # « veryfast » : ~3x plus rapide que « medium » pour une qualité visuellement proche
    # (fichier un peu plus lourd). Réglable avec RENDER_PRESET (ultrafast … slow).
    import os
    preset = os.environ.get("RENDER_PRESET", "veryfast")
    cmd += [
        # débit plafonné : ~5 Mb/s suffit largement pour un écran de téléphone (TikTok recompresse
        # de toute façon) et garde un clip de 60 s sous ~40 Mo, partageable depuis le téléphone
        "-c:v", "libx264", "-preset", preset, "-crf", os.environ.get("RENDER_CRF", "22"),
        "-maxrate", os.environ.get("RENDER_MAXRATE", "5M"), "-bufsize", "10M",
        "-pix_fmt", "yuv420p", "-r", "30", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", str(out),
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:  # ffmpeg écrit « out_time_us=… » au fil de l'encodage
        if on_progress and line.startswith("out_time_us=") and duration > 0:
            try:
                done = int(line.split("=", 1)[1]) / 1_000_000
            except ValueError:
                continue
            on_progress(max(0.0, min(1.0, done / duration)))
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise RuntimeError(f"échec du montage ffmpeg : {err.strip()[-400:]}")
    return out


def thumbnail(video: Path, out: Path, at: float = 1.0) -> Path:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{at:.2f}",
                    "-i", str(video), "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(out)],
                   check=True)
    return out
