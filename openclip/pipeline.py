"""Orchestration : lien YouTube -> clips -> (optionnel) publication."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import ClipSettings, Settings
from .download import local_source, safe_download
from .highlights import find_highlights
from .render import keep_intervals, remap_words, render_clip, thumbnail
from .subtitles import build_ass
from .transcribe import load as load_transcript
from .transcribe import transcribe, words_between

log = logging.getLogger(__name__)

# progress(étape, fraction 0-1, message)
Progress = Callable[[str, float, str], None]


def _noop(stage: str, frac: float, msg: str) -> None:
    log.info("[%s %3.0f%%] %s", stage, frac * 100, msg)


def _mmss(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def make_clips(url: str, settings: Settings, out_root: Path, progress: Progress = _noop,
               local_file: Path | None = None, local_title: str = "") -> Path:
    cs = settings.clip
    work = out_root / "_sources"
    if local_file:
        progress("download", 0.05, "Lecture du fichier envoyé")
        src = local_source(local_file, local_title or local_file.stem)
    else:
        progress("download", 0.02, "Téléchargement de la vidéo")
        src = safe_download(url, work)
    out_dir = out_root / src.video_id
    out_dir.mkdir(parents=True, exist_ok=True)

    progress("transcribe", 0.15, f"Préparation de la transcription de « {src.title} »")
    transcript = work / f"{src.video_id}.transcript.json"
    started = time.time()

    def on_transcribe(done: float, total: float) -> None:
        frac = done / total if total else 0
        msg = f"Transcription {_mmss(done)} / {_mmss(total)}"
        elapsed = time.time() - started
        if frac > 0.03:
            msg += f" · encore ~{_mmss(elapsed / frac - elapsed)}"
        progress("transcribe", 0.15 + 0.3 * frac, msg)

    segments = transcribe(src.path, transcript, settings.whisper_model,
                          settings.whisper_device, cs.language, progress=on_transcribe)

    progress("analyze", 0.45, f"Recherche des {cs.num_clips} meilleurs moments")
    highlights = find_highlights(segments, title=src.title, num_clips=cs.num_clips,
                                 min_s=cs.min_seconds, max_s=cs.max_seconds,
                                 model=settings.anthropic_model, media=src.path)
    if not highlights:
        raise RuntimeError("aucun passage exploitable (vidéo sans parole ?)")

    manifest = {
        "source": asdict(src) | {"path": str(src.path), "transcript": str(transcript)},
        "settings": asdict(cs),
        "created_at": time.time(),
        "clips": [],
    }
    ordered = sorted(highlights, key=lambda x: -x.score)
    for i, h in enumerate(ordered, 1):
        progress("render", 0.5 + 0.48 * (i - 1) / len(ordered), f"Montage du clip {i}/{len(ordered)}")
        clip = {"file": f"clip_{i:02d}.mp4", **asdict(h), "style": cs.caption_style, "uploads": {}}
        _render(clip, out_dir, src.path, segments, cs)
        manifest["clips"].append(clip)
        _save(out_dir, manifest)

    progress("done", 1.0, f"{len(ordered)} clips prêts")
    return out_dir


def _render(clip: dict, out_dir: Path, src: Path, segments, cs: ClipSettings) -> None:
    start, end = clip["start"], clip["end"]
    words = words_between(segments, start, end)
    intervals = keep_intervals(words, start, end) if cs.jump_cuts else [(start, end)]
    timeline = remap_words(words, intervals)
    duration = sum(b - a for a, b in intervals)
    stem = Path(clip["file"]).stem
    ass = None
    if cs.subtitles:
        ass = build_ass(timeline, 0.0, duration, out_dir / f"{stem}.ass", width=cs.width,
                        height=cs.height, hook=clip.get("hook", ""),
                        style=clip.get("style", cs.caption_style), keywords=clip.get("keywords"))
    mp4 = out_dir / clip["file"]
    render_clip(src, start, end, mp4, mode=cs.crop_mode, ass=ass, out_w=cs.width,
                out_h=cs.height, intervals=intervals, progress_bar=cs.progress_bar)
    thumbnail(mp4, out_dir / f"{stem}.jpg", at=min(1.0, duration / 2))
    clip["duration"] = round(duration, 2)
    clip["version"] = clip.get("version", 0) + 1


def rerender(clip_dir: Path, index: int, changes: dict) -> dict:
    """Applique des modifications (accroche, style, bornes…) et refait le montage d'un clip."""
    manifest = load_manifest(clip_dir)
    clip = manifest["clips"][index - 1]
    clip.update({k: v for k, v in changes.items()
                 if k in {"hook", "style", "keywords", "start", "end"}})
    cs = ClipSettings(**manifest.get("settings", {}))
    for key in ("crop_mode", "jump_cuts", "progress_bar", "subtitles"):
        if key in changes:
            setattr(cs, key, changes[key])
    segments = load_transcript(Path(manifest["source"]["transcript"]))
    _render(clip, clip_dir, Path(manifest["source"]["path"]), segments, cs)
    _save(clip_dir, manifest)
    return clip


def load_manifest(clip_dir: Path) -> dict:
    return json.loads((clip_dir / "clips.json").read_text())


def _save(clip_dir: Path, manifest: dict) -> None:
    tmp = clip_dir / "clips.json.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    tmp.replace(clip_dir / "clips.json")


def caption_for(clip: dict, credit: str = "") -> str:
    tags = " ".join(f"#{t}" for t in clip.get("hashtags", []))
    parts = [clip["title"], clip.get("description", ""), tags, credit]
    return "\n".join(p for p in parts if p).strip()


def publish_one(clip_dir: Path, index: int, settings: Settings, target: str, *,
                tiktok_mode: str = "direct", privacy: str | None = None) -> dict:
    manifest = load_manifest(clip_dir)
    clip = manifest["clips"][index - 1]
    source = manifest["source"]
    credit = f"🎬 Source : {source.get('uploader') or ''} {source.get('url', '')}".strip()
    video = clip_dir / clip["file"]
    if target == "tiktok":
        from .uploaders import tiktok
        res = tiktok.upload(settings, video, caption_for(clip, credit),
                            mode=tiktok_mode, privacy=privacy)
    elif target == "youtube":
        from .uploaders import youtube
        desc = caption_for({**clip, "title": ""}, credit)
        res = youtube.upload(settings, video, clip["title"], desc, clip.get("hashtags", []),
                             privacy=privacy or "public")
    else:
        raise ValueError(f"cible inconnue : {target}")
    res["at"] = time.time()
    manifest = load_manifest(clip_dir)  # relit au cas où le fichier a changé pendant l'envoi
    manifest["clips"][index - 1]["uploads"][target] = res
    _save(clip_dir, manifest)
    return res


def publish(clip_dir: Path, settings: Settings, targets: list[str], *,
            tiktok_mode: str = "direct", privacy: str | None = None,
            only: list[int] | None = None) -> None:
    manifest = load_manifest(clip_dir)
    for idx, clip in enumerate(manifest["clips"], 1):
        if only and idx not in only:
            continue
        for target in targets:
            if target in clip["uploads"]:
                log.info("%s déjà publié sur %s — ignoré", clip["file"], target)
                continue
            try:
                res = publish_one(clip_dir, idx, settings, target,
                                  tiktok_mode=tiktok_mode, privacy=privacy)
                log.info("📤 %s -> %s : %s", clip["file"], target, res)
            except Exception as exc:
                log.error("❌ %s -> %s : %s", clip["file"], target, exc)
