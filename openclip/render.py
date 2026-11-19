"""Découpe + recadrage 9:16 + sous-titres incrustés avec ffmpeg."""
from __future__ import annotations

import json
import re
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


def detect_facecam(path: Path, start: float, end: float, samples: int = 14):
    """Repère la caméra d'un streamer incrustée dans l'image (jeu vidéo + webcam).

    Indice : un visage *petit* (moins de ~18 % de la largeur) qui reste *au même endroit*
    pendant tout le passage. On cherche ensuite les bords du cadre de la caméra : ces bords
    sont fixes alors que le jeu bouge, donc ils ressortent quand on fait la moyenne des contours
    sur plusieurs images. Renvoie (x, y, w, h) en pixels de la vidéo source, ou None.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap = cv2.VideoCapture(str(path))
    faces, edges, size = [], None, None
    try:
        for i in range(samples):
            t = start + (end - start) * (i + 0.5) / samples
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            h, w = frame.shape[:2]
            size = (w, h)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found = cascade.detectMultiScale(gray, 1.1, 5, minSize=(max(24, h // 30),) * 2)
            small = [f for f in found if f[2] < 0.18 * w]
            if small:
                faces.append(max(small, key=lambda f: f[2] * f[3]))
            e = np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0)) + np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1))
            edges = e if edges is None else edges + e
    finally:
        cap.release()
    if not size or len(faces) < max(2, samples * 0.35):
        return None
    w, h = size
    arr = np.array(faces, dtype=float)
    cx, cy = np.median(arr[:, 0] + arr[:, 2] / 2), np.median(arr[:, 1] + arr[:, 3] / 2)
    fw, fh = np.median(arr[:, 2]), np.median(arr[:, 3])
    spread = np.median(np.hypot(arr[:, 0] + arr[:, 2] / 2 - cx, arr[:, 1] + arr[:, 3] / 2 - cy))
    if spread > 0.04 * w:  # le visage bouge : c'est un personnage du jeu, pas une caméra
        return None

    # bords du cadre : lignes de contours fortes et fixes autour du visage
    edges /= max(1, len(faces))
    vx = edges

    def strongest(profile, lo, hi, prefer):
        lo, hi = int(max(0, lo)), int(min(len(profile), hi))
        if hi - lo < 3:
            return None
        seg = profile[lo:hi]
        k = int(np.argmax(seg))
        return lo + k if seg[k] > prefer else None

    y0, y1 = int(max(0, cy - 2.5 * fh)), int(min(h, cy + 2.5 * fh))
    x0, x1 = int(max(0, cx - 3 * fw)), int(min(w, cx + 3 * fw))
    cols = vx[y0:y1].mean(axis=0)
    rows = vx[:, x0:x1].mean(axis=1)
    thr_c, thr_r = np.median(cols) * 2.2, np.median(rows) * 2.2
    left = strongest(cols, cx - 3 * fw, cx - 0.8 * fw, thr_c)
    right = strongest(cols, cx + 0.8 * fw, cx + 3 * fw, thr_c)
    top = strongest(rows, cy - 2.6 * fh, cy - 0.9 * fh, thr_r)
    bottom = strongest(rows, cy + 0.9 * fh, cy + 2.6 * fh, thr_r)
    # bords collés au bord de l'image ou introuvables : estimation à partir du visage
    left = 0 if cx - 3 * fw <= 0 and left is None else left
    left = left if left is not None else cx - 1.9 * fw
    right = right if right is not None else cx + 1.9 * fw
    top = top if top is not None else cy - 1.5 * fh
    bottom = bottom if bottom is not None else cy + 1.5 * fh
    x, y = int(max(0, left)), int(max(0, top))
    bw, bh = int(min(w, right) - x), int(min(h, bottom) - y)
    if bw < 2 * fw or bh < 1.5 * fh or bw > 0.6 * w or bh > 0.7 * h:
        return None
    return (x, y, bw // 2 * 2, bh // 2 * 2)


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
                loudnorm: bool = True, bar_color: str = "0xFFD400", cam=None) -> str:
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

    if mode == "stream" and cam:
        # caméra en haut (pleine largeur), jeu en bas : la mise en page des clips de stream
        x, y, cw, ch = cam
        top_h = int(min(max(out_w * ch / cw, out_h * 0.28), out_h * 0.42)) // 2 * 2
        want = out_w / top_h  # recadre la caméra au bon format, centré sur elle
        if cw / ch > want:
            nw = int(ch * want) // 2 * 2
            x, cw = x + (cw - nw) // 2, nw
        else:
            nh = int(cw / want) // 2 * 2
            y, ch = y + (ch - nh) // 2, nh
        bot_h = out_h - top_h
        gw = min(src_w, int(src_h * out_w / bot_h) // 2 * 2)
        gc = center_x if center_x is not None else 0.5
        gx = int(min(max(gc * src_w - gw / 2, 0), src_w - gw))
        # évite de remettre la caméra dans la partie jeu si c'est possible
        if gx < cam[0] + cam[2] and cam[0] < gx + gw:
            right_room, left_room = src_w - (cam[0] + cam[2]), cam[0]
            if right_room >= gw and cam[0] < src_w / 2:
                gx = max(cam[0] + cam[2], min(gx, src_w - gw))
            elif left_room >= gw:
                gx = min(cam[0] - gw, gx)
        chains.append(
            f"[vc]split=2[c][g];"
            f"[c]crop={cw}:{ch}:{x}:{y},scale={out_w}:{top_h},setsar=1[top];"
            f"[g]crop={gw}:{src_h}:{gx}:0,scale={out_w}:{bot_h},setsar=1[bot];"
            f"[top][bot]vstack=inputs=2,setsar=1[vf]"
        )
    elif mode == "blur":
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
    cam = None
    if mode in {"face", "stream"}:
        try:  # streamer avec caméra incrustée ? -> mise en page caméra + jeu
            cam = detect_facecam(src, start, end)
        except Exception as exc:
            log.warning("détection de la caméra impossible (%s)", exc)
        if cam:
            mode = "stream"
            crop_frac = min(1.0, src_h * out_w / (out_h * 0.66) / src_w)
            try:
                center = content_center_x(src, start, end, crop_frac)
            except Exception:
                center = None
        elif mode == "stream":
            mode = "face"
    if mode == "face":
        try:
            center = face_center_x(src, start, end)
            if center is None:  # pas de visage : on suit l'action
                crop_frac = min(1.0, src_h * out_w / out_h / src_w)
                center = content_center_x(src, start, end, crop_frac)
        except Exception as exc:  # la détection ne doit jamais empêcher le montage
            log.warning("analyse du cadrage impossible (%s) — recadrage centré", exc)
    if cam and ass and ass.exists():
        # mise en page caméra + jeu : l'accroche se place sur la jointure, pas sur le visage
        x_, y_, cw_, ch_ = cam
        top_h = int(min(max(out_w * ch_ / cw_, out_h * 0.28), out_h * 0.42))
        txt = ass.read_text(encoding="utf-8")
        txt = re.sub(r"^(Style: Hook,(?:[^,]*,){20})\d+", lambda m: m.group(1) + str(max(0, top_h - 70)),
                     txt, flags=re.M)
        ass.write_text(txt, encoding="utf-8")
    rel = [(a - start, b - start) for a, b in intervals] if intervals else None
    duration = sum(b - a for a, b in rel) if rel else end - start
    graph = build_graph(src_w, src_h, mode, center, out_w, out_h, ass, intervals=rel,
                        audio=audio, progress_bar=progress_bar, duration=duration,
                        loudnorm=loudnorm, cam=cam)
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
