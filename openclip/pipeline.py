"""Orchestration : lien YouTube -> clips -> (optionnel) publication."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .config import ClipSettings, Settings
from .download import download_captions, local_source, parse_json3, safe_download
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


def _caption_lang(captions: Path) -> str:
    import re as _re

    m = _re.search(r"\.captions\.([a-zA-Z-]+?)(?:-orig)?\.json3$", captions.name)
    return (m.group(1) if m else "").split("-")[0].lower()


def _captions_language_ok(captions: Path, src, settings: Settings, progress: Progress) -> bool:
    """Vérifie que la langue des sous-titres YouTube est bien celle parlée dans la vidéo.

    Si le créateur a mal réglé la langue de sa vidéo, YouTube transcrit par exemple du
    français avec son modèle anglais : on obtient des phrases anglaises sans aucun sens.
    """
    cap_lang = _caption_lang(captions)
    wanted = (settings.clip.language or "").lower()
    if wanted:
        ok = cap_lang == wanted
    else:
        progress("transcribe", 0.13, "Vérification de la langue parlée")
        try:
            from .transcribe import detect_spoken_language
            spoken, prob = detect_spoken_language(src.path, src.duration, settings.whisper_model,
                                                  settings.whisper_device)
        except Exception as exc:  # pas de modèle disponible : on se fie au titre
            log.warning("détection de langue impossible (%s)", exc)
            spoken, prob = "", 0.0
        if prob >= 0.5:
            ok = cap_lang == spoken
        else:  # détection incertaine : on compare avec la langue du titre de la vidéo
            from .analyzer import title_language
            ok = title_language(src.title) in {"", cap_lang}
        if not ok:
            log.info("sous-titres YouTube en %s mais vidéo en %s (%.0f %%) : transcription locale",
                     cap_lang, spoken, prob * 100)
    if not ok:
        progress("transcribe", 0.14, "Sous-titres YouTube dans la mauvaise langue : transcription par OpenClip")
    return ok


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

    transcript = work / f"{src.video_id}.transcript.json"
    meta_file = work / f"{src.video_id}.transcript.meta.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    if transcript.exists() and not local_file:
        wanted = (cs.language or "").lower()
        legacy = not meta and any(work.glob(f"{src.video_id}.captions.*json3"))
        wrong_lang = meta.get("source") == "youtube" and wanted and meta.get("lang") != wanted
        if legacy or wrong_lang:
            # transcription faite par une ancienne version (sous-titres YouTube non vérifiés)
            # ou dans une autre langue que celle demandée : on la vérifie / refait
            cap = next(iter(sorted(work.glob(f"{src.video_id}.captions.*json3"))), None)
            if wrong_lang or not cap or not _captions_language_ok(cap, src, settings, progress):
                transcript.unlink(missing_ok=True)
            else:
                meta = {"source": "youtube", "lang": _caption_lang(cap), "checked": True}
                meta_file.write_text(json.dumps(meta))
    if not transcript.exists() and not local_file and settings.youtube_captions:
        # le plus rapide : les sous-titres automatiques de YouTube (quelques secondes)
        progress("transcribe", 0.12, "Récupération des sous-titres YouTube")
        captions = download_captions(url, work, src.video_id)
        if captions and not _captions_language_ok(captions, src, settings, progress):
            captions = None
        if captions:
            try:
                segs = parse_json3(captions)
            except (ValueError, KeyError):
                segs = []
            if len(segs) >= 5:
                transcript.write_text(json.dumps([asdict(x) for x in segs], ensure_ascii=False))
                meta_file.write_text(json.dumps({"source": "youtube", "lang": _caption_lang(captions),
                                                 "checked": True}))
                progress("transcribe", 0.45, f"Sous-titres YouTube récupérés ({len(segs)} phrases)")

    if not transcript.exists():
        progress("transcribe", 0.15, f"Préparation de la transcription de « {src.title} »")
    started = time.time()

    def on_transcribe(done: float, total: float) -> None:
        frac = done / total if total else 0
        msg = f"Transcription {_mmss(done)} / {_mmss(total)}"
        elapsed = time.time() - started
        if frac > 0.03:
            msg += f" · encore ~{_mmss(elapsed / frac - elapsed)}"
        progress("transcribe", 0.15 + 0.3 * frac, msg)

    fresh = not transcript.exists()
    segments = transcribe(src.path, transcript, settings.whisper_model,
                          settings.whisper_device, cs.language, progress=on_transcribe)
    if fresh:
        meta_file.write_text(json.dumps({"source": "whisper", "lang": cs.language or ""}))

    existing = load_manifest(out_dir) if (out_dir / "clips.json").exists() else None
    exclude = [(c["start"], c["end"]) for c in existing["clips"]] if existing else []
    already = f" (en évitant les {len(exclude)} déjà montés)" if exclude else ""
    progress("analyze", 0.45, f"Recherche de {cs.num_clips} moments forts{already}")
    highlights = find_highlights(segments, title=src.title, num_clips=cs.num_clips,
                                 min_s=cs.min_seconds, max_s=cs.max_seconds,
                                 model=settings.anthropic_model, media=src.path, exclude=exclude)
    if not highlights:
        if existing:
            raise RuntimeError("plus de nouveau passage à monter : tous les moments de cette durée "
                               "ont déjà été utilisés (essaie une autre durée)")
        raise RuntimeError("aucun passage exploitable (vidéo sans parole ?)")

    if existing:
        manifest = existing
        manifest["source"].update({"path": str(src.path), "transcript": str(transcript)})
    else:
        manifest = {
            "source": asdict(src) | {"path": str(src.path), "transcript": str(transcript)},
            "settings": asdict(cs),
            "created_at": time.time(),
            "clips": [],
        }
    manifest["settings"] = asdict(cs)
    import re as _re
    numbers = [int(m.group(1)) for c in manifest["clips"] if (m := _re.search(r"clip_(\d+)", c["file"]))]
    first = max(numbers, default=0) + 1
    ordered = sorted(highlights, key=lambda x: -x.score)
    for i, h in enumerate(ordered, 1):
        base = 0.5 + 0.48 * (i - 1) / len(ordered)
        progress("render", base, f"Montage du clip {i}/{len(ordered)}")

        def on_clip(frac: float, i=i, base=base) -> None:
            progress("render", base + 0.48 * frac / len(ordered),
                     f"Montage du clip {i}/{len(ordered)} · {round(frac * 100)} %")

        clip = {"file": f"clip_{first + i - 1:02d}.mp4", **asdict(h), "style": cs.caption_style,
                "uploads": {}, "created_at": time.time()}
        _render(clip, out_dir, src.path, segments, cs, on_progress=on_clip)
        manifest["clips"].append(clip)
        _save(out_dir, manifest)

    msg = f"{len(ordered)} nouveaux clips (total {len(manifest['clips'])})" if existing else f"{len(ordered)} clips prêts"
    if len(ordered) < cs.num_clips:
        msg += f" (vidéo trop courte pour {cs.num_clips} clips de {cs.min_seconds:.0f} s minimum)"
    progress("done", 1.0, msg)
    return out_dir


def _render(clip: dict, out_dir: Path, src: Path, segments, cs: ClipSettings, on_progress=None) -> None:
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
                out_h=cs.height, intervals=intervals, progress_bar=cs.progress_bar,
                on_progress=on_progress)
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


def delete_clip(clip_dir: Path, index: int) -> None:
    """Supprime un clip (fichiers + entrée) et met à jour la file d'attente automatique."""
    manifest = load_manifest(clip_dir)
    clip = manifest["clips"].pop(index - 1)
    stem = Path(clip["file"]).stem
    for ext in (".mp4", ".ass", ".jpg"):
        (clip_dir / f"{stem}{ext}").unlink(missing_ok=True)
    _save(clip_dir, manifest)
    from . import automation
    with automation._lock:
        queue = []
        for q in automation.load_queue():
            if q["project"] == clip_dir.name:
                if q["clip"] == index:
                    continue
                if q["clip"] > index:
                    q["clip"] -= 1
            queue.append(q)
        automation._save_queue(queue)


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
                tiktok_mode: str = "direct", privacy: str | None = None, account: str = "main",
                upload_post_user: str | None = None) -> dict:
    manifest = load_manifest(clip_dir)
    clip = manifest["clips"][index - 1]
    source = manifest["source"]
    credit = f"🎬 Source : {source.get('uploader') or ''} {source.get('url', '')}".strip()
    video = clip_dir / clip["file"]
    via = {x.strip() for x in settings.upload_post_platforms.split(",") if x.strip()}
    if target in via and settings.upload_post_key:
        from .uploaders import uploadpost
        desc = caption_for({**clip, "title": ""}, credit)
        res = uploadpost.upload(settings.upload_post_key, upload_post_user or settings.upload_post_user, video, [target],
                                caption_for(clip, credit) if target == "tiktok" else clip["title"], desc)
    elif target == "tiktok":
        from .uploaders import tiktok
        res = tiktok.upload(settings, video, caption_for(clip, credit),
                            mode=tiktok_mode, privacy=privacy, account=account)
    elif target == "youtube":
        from .uploaders import youtube
        desc = caption_for({**clip, "title": ""}, credit)
        res = youtube.upload(settings, video, clip["title"], desc, clip.get("hashtags", []),
                             privacy=privacy or "public")
    else:
        raise ValueError(f"cible inconnue : {target}")
    res["at"] = time.time()
    manifest = load_manifest(clip_dir)  # relit au cas où le fichier a changé pendant l'envoi
    key = target if account == "main" else f"{target}:{account}"
    manifest["clips"][index - 1]["uploads"][key] = {**res, "account": account}
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
