"""Orchestration : lien YouTube -> clips -> (optionnel) publication."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .download import download
from .highlights import find_highlights
from .render import render_clip
from .subtitles import build_ass
from .transcribe import transcribe, words_between

log = logging.getLogger(__name__)


def make_clips(url: str, settings: Settings, out_root: Path) -> Path:
    cs = settings.clip
    work = out_root / "_sources"
    log.info("Téléchargement de %s", url)
    src = download(url, work)
    out_dir = out_root / src.video_id
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("Transcription (%s) de « %s »", settings.whisper_model, src.title)
    segments = transcribe(src.path, work / f"{src.video_id}.transcript.json",
                          settings.whisper_model, settings.whisper_device, cs.language)

    log.info("Recherche des %d meilleurs moments", cs.num_clips)
    highlights = find_highlights(segments, title=src.title, num_clips=cs.num_clips,
                                 min_s=cs.min_seconds, max_s=cs.max_seconds,
                                 model=settings.anthropic_model)
    if not highlights:
        raise RuntimeError("aucun passage exploitable (vidéo sans parole ?)")

    manifest = {"source": asdict(src) | {"path": str(src.path)}, "clips": []}
    for i, h in enumerate(sorted(highlights, key=lambda x: x.start), 1):
        name = f"clip_{i:02d}"
        ass = None
        if cs.subtitles:
            ass = build_ass(words_between(segments, h.start, h.end), h.start, h.end,
                            out_dir / f"{name}.ass", width=cs.width, height=cs.height, hook=h.hook)
        mp4 = out_dir / f"{name}.mp4"
        log.info("Rendu %s (%.0f s, score %.0f) : %s", name, h.duration, h.score, h.title)
        render_clip(src.path, h.start, h.end, mp4, mode=cs.crop_mode, ass=ass,
                    out_w=cs.width, out_h=cs.height)
        manifest["clips"].append({"file": mp4.name, **asdict(h), "uploads": {}})

    (out_dir / "clips.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    log.info("✅ %d clips dans %s", len(highlights), out_dir)
    return out_dir


def caption_for(clip: dict, credit: str = "") -> str:
    tags = " ".join(f"#{t}" for t in clip.get("hashtags", []))
    parts = [clip["title"], clip.get("description", ""), tags, credit]
    return "\n".join(p for p in parts if p).strip()


def publish(clip_dir: Path, settings: Settings, targets: list[str], *,
            tiktok_mode: str = "direct", privacy: str | None = None,
            only: list[int] | None = None) -> None:
    manifest_path = clip_dir / "clips.json"
    manifest = json.loads(manifest_path.read_text())
    source = manifest["source"]
    credit = f"🎬 Source : {source.get('uploader') or ''} {source.get('url', '')}".strip()

    for idx, clip in enumerate(manifest["clips"], 1):
        if only and idx not in only:
            continue
        video = clip_dir / clip["file"]
        for target in targets:
            if target in clip["uploads"]:
                log.info("%s déjà publié sur %s — ignoré", clip["file"], target)
                continue
            try:
                if target == "tiktok":
                    from .uploaders import tiktok
                    res = tiktok.upload(settings, video, caption_for(clip, credit),
                                        mode=tiktok_mode, privacy=privacy)
                elif target == "youtube":
                    from .uploaders import youtube
                    desc = caption_for({**clip, "title": ""}, credit)
                    res = youtube.upload(settings, video, clip["title"], desc,
                                         clip.get("hashtags", []), privacy=privacy or "public")
                else:
                    raise ValueError(f"cible inconnue : {target}")
            except Exception as exc:
                log.error("❌ %s -> %s : %s", clip["file"], target, exc)
                continue
            clip["uploads"][target] = res
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            log.info("📤 %s -> %s : %s", clip["file"], target, res)


