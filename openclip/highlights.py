"""Sélection des meilleurs moments (30-60 s) par Claude, avec repli heuristique."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace

from .transcribe import Segment

log = logging.getLogger(__name__)


@dataclass
class Highlight:
    start: float
    end: float
    title: str
    hook: str = ""
    description: str = ""
    hashtags: list[str] = field(default_factory=list)
    score: float = 0.0
    keywords: list[str] = field(default_factory=list)
    hook_score: float = 0.0
    retention_score: float = 0.0
    share_score: float = 0.0
    reason: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


SYSTEM_PROMPT = """Tu es un monteur expert en contenus courts qui performent (TikTok, YouTube Shorts,
Reels) pour des créateurs qui vivent de leurs vues. On te donne la transcription horodatée d'une vidéo.
Choisis les passages qui feront les clips verticaux les plus regardés jusqu'au bout et les plus partagés.

Ce qui fait un clip qui rapporte :
- Accroche dans les 2 premières secondes : question intrigante, affirmation choc, chiffre surprenant,
  conflit, début d'histoire. Si la meilleure accroche est au milieu du passage, commence le clip là.
- Une seule idée, compréhensible sans le reste de la vidéo (pas de « comme je disais », « lui »…).
- Une tension qui donne envie de rester (boucle ouverte, suspense, liste, avant/après) et une chute :
  révélation, punchline, conseil concret, émotion. Jamais de fin au milieu d'une phrase.
- Valeur claire pour le spectateur : il apprend, rit, est surpris, ou a envie de commenter.
- Évite les passages de remerciements, sponsors, intros/outros et passages qui dépendent d'un visuel.

Pour chaque clip :
- `hook` : texte affiché en haut de l'écran les 3 premières secondes. 4 à 9 mots, percutant, qui crée
  la curiosité sans tout dévoiler (pas de simple résumé). Un emoji maximum.
- `title` : titre court pour la publication. `description` : 1-2 phrases qui poussent à commenter
  (une question au spectateur marche bien).
- `hashtags` : 3 à 6, mélange de niche et de large, sans le #.
- `keywords` : 3 à 8 mots EXACTS prononcés dans le passage qui portent l'émotion ou l'info
  (chiffres, noms, mots forts). Ils seront colorés dans les sous-titres.
- Notes de 0 à 100 : `hook_score` (force du début), `retention_score` (envie de regarder jusqu'au bout),
  `share_score` (envie de partager/commenter), et `score` = potentiel viral global.
- `reason` : une phrase qui explique pourquoi ce passage va marcher.

Écris dans la langue de la vidéo. Respecte strictement la durée demandée. Les clips ne doivent pas se
chevaucher. Sois exigeant dans les notes : un passage moyen vaut 40-60, réserve 85+ aux pépites."""

_CLIP_PROPS = {
    "start": {"type": "number", "description": "début en secondes"},
    "end": {"type": "number", "description": "fin en secondes"},
    "title": {"type": "string"},
    "hook": {"type": "string"},
    "description": {"type": "string"},
    "hashtags": {"type": "array", "items": {"type": "string"}},
    "keywords": {"type": "array", "items": {"type": "string"}},
    "score": {"type": "number"},
    "hook_score": {"type": "number"},
    "retention_score": {"type": "number"},
    "share_score": {"type": "number"},
    "reason": {"type": "string"},
}

SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _CLIP_PROPS,
                "required": list(_CLIP_PROPS),
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


def format_transcript(segments: list[Segment]) -> str:
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in segments)


def find_highlights(
    segments: list[Segment],
    *,
    title: str,
    num_clips: int,
    min_s: float,
    max_s: float,
    model: str = "claude-opus-5-5",
) -> list[Highlight]:
    if not segments:
        return []
    try:
        raw = _ask_claude(segments, title, num_clips, min_s, max_s, model)
    except Exception as exc:  # pas de clé, réseau, refus...
        log.warning("Claude indisponible (%s) — repli sur l'heuristique.", exc)
        raw = heuristic(segments, num_clips, min_s, max_s)
    clips = [snap(h, segments, min_s, max_s) for h in raw]
    return remove_overlaps(sorted(clips, key=lambda h: -h.score))[:num_clips]


def _ask_claude(segments, title, num_clips, min_s, max_s, model) -> list[Highlight]:
    import anthropic

    client = anthropic.Anthropic()
    user = (
        f"Titre de la vidéo : {title}\n"
        f"Propose les {num_clips} meilleurs clips, chacun entre {min_s:.0f} et {max_s:.0f} secondes.\n\n"
        f"<transcription>\n{format_transcript(segments)}\n</transcription>"
    )
    with client.beta.messages.stream(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": user}],
    ) as stream:
        response = stream.get_final_message()

    if response.stop_reason == "refusal":
        raise RuntimeError("requête refusée par le modèle")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("réponse tronquée (max_tokens)")
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    return [
        Highlight(
            start=float(c["start"]),
            end=float(c["end"]),
            title=c["title"],
            hook=c["hook"],
            description=c["description"],
            hashtags=[t.lstrip("#").replace(" ", "") for t in c["hashtags"]],
            score=float(c["score"]),
            keywords=c["keywords"],
            hook_score=float(c["hook_score"]),
            retention_score=float(c["retention_score"]),
            share_score=float(c["share_score"]),
            reason=c["reason"],
        )
        for c in data["clips"]
    ]


def heuristic(segments: list[Segment], num_clips: int, min_s: float, max_s: float) -> list[Highlight]:
    """Repli sans IA : fenêtres glissantes notées sur la densité de parole."""
    target = (min_s + max_s) / 2
    candidates: list[Highlight] = []
    for i, seg in enumerate(segments):
        start = seg.start
        words, end = 0, start
        for s in segments[i:]:
            if s.end - start > max_s:
                break
            words += len(s.text.split())
            end = s.end
        dur = end - start
        if dur < min_s:
            continue
        density = words / dur
        punch = sum(segments[i].text.count(c) for c in "?!")
        score = density * 10 + punch * 5 - abs(dur - target) * 0.2
        candidates.append(Highlight(start, end, seg.text[:60] or "Clip", hook=seg.text[:80],
                                    score=max(0.0, min(100.0, score)), reason="Choix automatique (sans IA)"))
    return remove_overlaps(sorted(candidates, key=lambda h: -h.score))[:num_clips]


def snap(h: Highlight, segments: list[Segment], min_s: float, max_s: float) -> Highlight:
    """Aligne début/fin sur des frontières de phrase et force la durée dans [min_s, max_s]."""
    starts = [s.start for s in segments]
    ends = [s.end for s in segments]
    start = min(starts, key=lambda t: abs(t - h.start))
    end_ok = [e for e in ends if min_s <= e - start <= max_s]
    if end_ok:
        end = min(end_ok, key=lambda t: abs(t - h.end))
    else:  # aucune fin de phrase compatible : on coupe net
        end = start + min(max(h.end - start, min_s), max_s)
    end = min(end + 0.3, ends[-1] + 0.3)  # petite marge pour ne pas couper le dernier mot
    return replace(h, start=max(0.0, start - 0.1), end=end)


def remove_overlaps(clips: list[Highlight]) -> list[Highlight]:
    kept: list[Highlight] = []
    for c in clips:
        if all(c.end <= k.start or c.start >= k.end for k in kept):
            kept.append(c)
    return kept
