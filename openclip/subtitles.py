"""Sous-titres « style TikTok » : 3 mots à l'écran, mot prononcé surligné."""
from __future__ import annotations

from pathlib import Path

from .transcribe import Word

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,Montserrat ExtraBold,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,6,2,2,60,60,{margin},1
Style: Hook,Montserrat ExtraBold,{hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,-1,0,0,0,100,100,0,0,3,18,0,8,80,80,220,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

HIGHLIGHT = r"{\c&H00FFFF&\fscx110\fscy110}"  # jaune (BGR) + léger zoom
RESET = r"{\c&HFFFFFF&\fscx100\fscy100}"


def ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def chunk_words(words: list[Word], per_line: int = 3, max_gap: float = 0.6) -> list[list[Word]]:
    groups: list[list[Word]] = []
    for w in words:
        if (
            groups
            and len(groups[-1]) < per_line
            and w.start - groups[-1][-1].end <= max_gap
            and not groups[-1][-1].text.endswith((".", "?", "!"))
        ):
            groups[-1].append(w)
        else:
            groups.append([w])
    return groups


def build_ass(
    words: list[Word],
    clip_start: float,
    clip_end: float,
    out: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    hook: str = "",
    hook_seconds: float = 3.0,
) -> Path:
    duration = clip_end - clip_start
    lines = [HEADER.format(w=width, h=height, size=int(height * 0.045),
                           hook_size=int(height * 0.032), margin=int(height * 0.28))]
    if hook:
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(hook_seconds, duration))},Hook,,0,0,0,,{escape(hook)}")

    for group in chunk_words(words):
        for i, w in enumerate(group):
            start = w.start - clip_start
            end = (group[i + 1].start if i + 1 < len(group) else w.end) - clip_start
            if end <= 0 or start >= duration:
                continue
            parts = [
                (HIGHLIGHT + escape(x.text).upper() + RESET) if j == i else escape(x.text).upper()
                for j, x in enumerate(group)
            ]
            lines.append(f"Dialogue: 0,{ts(start)},{ts(min(end, duration))},Cap,,0,0,0,,{' '.join(parts)}")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
