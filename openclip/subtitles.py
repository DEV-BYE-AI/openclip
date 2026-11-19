"""Sous-titres animés façon contenus courts, en plusieurs styles.

Couleurs ASS au format &HAABBGGRR (alpha, bleu, vert, rouge).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .transcribe import Word


@dataclass(frozen=True)
class CaptionStyle:
    label: str
    font: str = "Montserrat ExtraBold"
    size: float = 0.045           # en fraction de la hauteur
    primary: str = "&H00FFFFFF"
    outline: str = "&H00000000"
    back: str = "&H80000000"
    outline_w: int = 6
    shadow: int = 2
    border_style: int = 1          # 1 = contour, 3 = boîte opaque
    uppercase: bool = True
    active: str = r"\c&H00FFFF&\fscx112\fscy112"   # mot prononcé
    keyword: str = r"\c&H5CF25C&"                   # mots-clés choisis par l'IA
    per_line: int = 3
    reveal: bool = False           # True = les mots apparaissent un par un
    extra: str = ""                # tags appliqués à toute la ligne
    position: float = 0.28         # marge basse en fraction de la hauteur


STYLES: dict[str, CaptionStyle] = {
    "pop": CaptionStyle("Pop — jaune + mots-clés verts"),
    "boxed": CaptionStyle(
        "Encadré — bandeau sombre", border_style=3, outline="&H40101010", outline_w=14, shadow=0,
        active=r"\c&H36A5FF&", keyword=r"\c&H36A5FF&", size=0.04,
    ),
    "neon": CaptionStyle(
        "Néon — lueur cyan", primary="&H00FFF04D", outline="&H00FF2DB4", outline_w=4, shadow=0,
        active=r"\c&HFFFFFF&\fscx115\fscy115", keyword=r"\c&H66CCFF&", extra=r"\blur3",
    ),
    "karaoke": CaptionStyle(
        "Karaoké — mots qui apparaissent", per_line=4, reveal=True,
        active=r"\c&H3C64FF&\fscx110\fscy110", keyword=r"\c&H3C64FF&",
    ),
    "minimal": CaptionStyle(
        "Minimal — sobre", font="Inter SemiBold", size=0.034, uppercase=False, outline_w=0,
        shadow=3, active="", keyword=r"\c&H00D7FF&", per_line=5, position=0.2,
    ),
}

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{primary},&H00FFFFFF,{outline},{back},-1,0,0,0,100,100,0,0,{bs},{ow},{sh},2,70,70,{margin},1
Style: Hook,Montserrat ExtraBold,{hook_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,22,0,8,90,90,230,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def escape(text: str) -> str:
    # le moteur de sous-titres (libass) ne sait pas dessiner les emojis en couleur : ils
    # apparaîtraient comme des carrés. On les retire de l'image (ils restent dans le titre).
    text = _EMOJI.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower())


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
    style: str = "pop",
    keywords: list[str] | None = None,
) -> Path:
    st = STYLES.get(style, STYLES["pop"])
    keys = {_norm(k) for kw in (keywords or []) for k in kw.split()} - {""}
    duration = clip_end - clip_start
    lines = [HEADER.format(
        w=width, h=height, font=st.font, size=int(height * st.size), primary=st.primary,
        outline=st.outline, back=st.back, bs=st.border_style, ow=st.outline_w, sh=st.shadow,
        margin=int(height * st.position), hook_size=int(height * 0.03),
    )]
    if hook:
        # léger effet d'apparition (fondu + zoom) pour attirer l'œil dès la première image
        anim = r"{\fad(150,250)\fscx90\fscy90\t(0,180,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 1,{ts(0)},{ts(min(hook_seconds, duration))},Hook,,0,0,0,,{anim}{escape(hook)}")

    def fmt(w: Word) -> str:
        t = escape(w.text)
        return t.upper() if st.uppercase else t

    for group in chunk_words(words, st.per_line):
        for i, w in enumerate(group):
            start = w.start - clip_start
            end = (group[i + 1].start if i + 1 < len(group) else w.end) - clip_start
            if end <= 0 or start >= duration:
                continue
            visible = group[: i + 1] if st.reveal else group
            parts = []
            for j, x in enumerate(visible):
                if j == i and st.active:
                    parts.append("{" + st.active + "}" + fmt(x) + "{\\r}")
                elif _norm(x.text) in keys:
                    parts.append("{" + st.keyword + "}" + fmt(x) + "{\\r}")
                else:
                    parts.append(fmt(x))
            # ligne trop longue pour l'écran (≈ 0,6 × taille de police par caractère) : on réduit
            chars = sum(len(x.text) for x in visible) + len(visible) - 1
            size = int(height * st.size)
            room = width - 2 * 70
            fit = "" if chars * 0.62 * size <= room else f"\\fs{max(int(room / (chars * 0.62)), int(size * 0.6))}"
            prefix = "{" + st.extra + fit + "}" if (st.extra or fit) else ""
            text = prefix + " ".join(parts)
            if st.extra or fit:  # \r remet le style à zéro : on réapplique les effets globaux
                text = text.replace("{\\r}", "{\\r" + st.extra + fit + "}")
            lines.append(f"Dialogue: 0,{ts(start)},{ts(min(end, duration))},Cap,,0,0,0,,{text}")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
