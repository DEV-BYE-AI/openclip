"""Analyse locale des moments forts — gratuite, sans IA externe.

Chaque passage candidat (30-60 s, aligné sur des phrases) est noté sur trois axes,
comme le ferait un monteur :

- Accroche  : le début donne-t-il envie de rester ? (question, chiffre, phrase choc,
              « tu/vous », voix énergique, pas de « et donc… » ni de « il » sans contexte)
- Rétention : rythme de parole, peu de blancs, énergie qui varie, fin sur une phrase complète
- Partage   : émotion, chiffres, adresse directe, rires, vocabulaire spécifique au sujet

On en déduit aussi l'accroche affichée à l'écran, le titre, la description, les hashtags,
les mots à colorer et une explication lisible.
"""
from __future__ import annotations

import math
import re
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path

from .transcribe import Segment

# --------------------------------------------------------------------------- lexiques

STOP = set("""
le la les un une des de du d l à a au aux et ou mais donc or ni car que qui quoi dont où ce cet cette ces
c ça cela il elle ils elles on je j tu nous vous me m te t se s lui leur leurs mon ma mes ton ta tes son sa
ses notre nos votre vos en y ne n pas plus moins très trop tout tous toute toutes est sont était être avoir
ai as avons avez ont eu été fait faire dit dire va vais vas aller comme pour par sur sous dans avec sans
chez entre si oui non bon bah ben euh hein voilà alors après avant aussi encore même quand comment pourquoi
parce peu bien vraiment juste genre truc chose choses fois là ici ah oh ok okay
the a an and or but so of to in on at for with without from by as is are was were be been being have has
had do does did not no yes it its this that these those there here i you he she we they me him her us them
my your his our their what which who whom when where why how just like really very so too also then than
um uh yeah well gonna wanna got get gets thing things
jour jours mois an ans année années temps gens monde personne vrai vraie fait faut peut veux veut
sais sait pense crois vois voit mettre prendre donner venir voir savoir pouvoir vouloir falloir
autre autres beaucoup chaque quelque quelques rien quelqu ensuite toujours jamais aujourd hui
moment façon manière exemple niveau point partie début fin coin cas voici comprendre
day days month months year years time people world person true make made know think see want
going come take give other others every some something nothing always never today way part
""".split())

HOOK_PATTERNS: list[tuple[str, float]] = [
    # français
    (r"\bpersonne ne\b", 3), (r"\ble (vrai )?secret\b", 3), (r"\bla vérité\b", 3),
    (r"\bvoici (comment|pourquoi|ce)\b", 2.5), (r"\b(jamais|personne)\b", 1.2),
    (r"\b(erreur|piège|arnaque|danger|interdit)\w*\b", 2.5), (r"\bil faut que tu\b", 2),
    (r"\b(imagine|écoute|regarde|attention)\b", 2), (r"\bça a (tout )?changé\b", 2.5),
    (r"\bje (vais|veux) (te|vous) (dire|montrer|expliquer)\b", 2.5),
    (r"\b(choquant|incroyable|fou|dingue|énorme|ouf)\b", 2), (r"\bla meilleure?\b", 1.5),
    (r"\b(pourquoi|comment)\b", 1.2), (r"\bsi tu\b", 1.5), (r"\b(premier|dernière?)\b", 0.8),
    (r"\b(gagner|perdre|argent|riche|millions?|milliards?|euros?)\b", 1.8),
    # anglais
    (r"\bnobody\b", 3), (r"\b(the )?secret\b", 3), (r"\bthe truth\b", 3),
    (r"\bhere'?s (how|why|what)\b", 2.5), (r"\b(never|nobody|no one)\b", 1.2),
    (r"\b(mistake|trap|scam|danger|banned)\w*\b", 2.5), (r"\byou need to\b", 2),
    (r"\b(imagine|listen|look|watch)\b", 2), (r"\bchanged (everything|my life)\b", 2.5),
    (r"\b(insane|crazy|unbelievable|shocking|huge)\b", 2), (r"\bthe best\b", 1.5),
    (r"\b(why|how)\b", 1.2), (r"\bif you\b", 1.5),
    (r"\b(money|rich|million|billion|dollars?)\b", 1.8),
]
HOOK_RE = [(re.compile(p, re.I), w) for p, w in HOOK_PATTERNS]

EMOTION = set("""
incroyable fou folle dingue énorme ouf choc choquant peur mort mourir pleurer pleuré rire drôle génial
magnifique horrible terrible catastrophe honte fier fière amour aimer déteste haine colère énervé
stress panique absolument complètement totalement
insane crazy amazing awesome terrible horrible scared fear dead die cry cried laugh funny love hate angry
shocked shocking incredible unbelievable never always absolutely completely totally seriously wow
""".split())

YOU = re.compile(r"\b(tu|toi|ton|ta|tes|vous|votre|vos|you|your)\b", re.I)
LAUGH = re.compile(r"\((rires?|laughs?|laughter)\)|\[(rires?|laughs?|laughter)\]|\b(haha+|mdr|lol)\b", re.I)
NUMBER = re.compile(r"\b\d[\d\s.,]*(%|€|k|m|ans?|jours?|euros?|dollars?)?\b", re.I)
CTA = re.compile(r"abonne|abonnez|like|cloche|sponsor|partenaire|code promo|lien en description|"
                 r"merci d'avoir regardé|subscribe|link in|description below|thanks for watching",
                 re.I)
BAD_START = re.compile(r"^(et|mais|donc|alors|parce que|du coup|bref|enfin|sinon|"
                       r"and|but|so|because|anyway|also)\b", re.I)
VAGUE_START = re.compile(r"^(il|elle|ils|elles|ça|ce|cela|lui|celui|celle|he|she|it|they|that|this)\b",
                         re.I)

EMOJI = {"question": "🤔", "money": "💰", "shock": "😳", "default": "👀"}


# --------------------------------------------------------------------------- utilitaires

def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\wÀ-ÿ]+", text.lower().replace("’", "'")) if t]


def _fold(word: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", word) if unicodedata.category(c) != "Mn")


def detect_language(segments: list[Segment]) -> str:
    words = Counter(t for s in segments[:200] for t in _tokens(s.text))
    fr = sum(words[w] for w in ("le", "la", "les", "je", "tu", "est", "pas", "que", "et", "des", "une"))
    en = sum(words[w] for w in ("the", "and", "you", "is", "that", "to", "of", "it", "i", "a"))
    return "fr" if fr >= en else "en"


# mots trop vagues pour faire un hashtag (en plus de STOP)
VAGUE = set("""
about above across actually after again against almost along already although always among another
anything anyway around aspect aspects basically because become before being below between beyond both
cannot certain certainly could course different during each either else enough especially even ever
every everything everyone example exactly fact feel feeling find follow following forward front further
going gonna great having however huge idea impression instead itself kind kinda least less like likely
little looking lots made make making many matter maybe mean might mind more most much must need never
next nothing obviously often once only other otherwise part perhaps place point pretty probably quite
rather really reason require required right same seem seems several should show since something sometimes
soon sort start still such sure than thank thanks their them then there these thing things think those
though thought through throughout today together toward towards under until upon used using usually
version very want wanted watch well were whatever when where whether which while whole whose will within
without would yeah year years your yourself literally definitely absolutely honestly completely totally
alors après aussi autre autres avant avoir beaucoup bien bref ceci cela celle celui cependant certains
chaque comme comment dans dedans dehors depuis déjà donc dont elle encore enfin ensemble entre être fait
faire faut fois genre gros grand grande ici jamais juste leur lors mais même moins parce partout pendant
peut plus plutôt pour pourquoi quand quelque rien sans selon seulement sinon sont sous souvent surtout
tellement tout toute toutes toujours très trop truc vraiment voilà voici vous nous leurs cette avec
review reviews video videos vidéo vidéos test official officiel trailer bande annonce episode épisode
partie part full complet complete live stream new nouveau nouvelle fini finis finie premier première
premiers premières dernier dernière derniers dernières vrai vraie meilleur meilleure pire seul seule
ligne face details détails revealed leaked games jeux jeu
please stay guys okay alright bro bruh dude hello hey man yeah wait look come stop chat chatter
merci salut mec frère wesh allez attends regarde viens
""".split())

_ELISION = re.compile(r"^(?:[ljdcnmst]|qu|jusqu|lorsqu|puisqu)['’]", re.I)

ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9", "x": "10"}


# noms longs connus -> forme courte utilisée sur les réseaux
ALIASES = [(re.compile(r"grand\s+theft\s+auto", re.I), "GTA"), (re.compile(r"red\s+dead\s+redemption", re.I), "RDR"),
           (re.compile(r"call\s+of\s+duty", re.I), "COD")]


def _normalize(text: str) -> str:
    for rx, short in ALIASES:
        text = rx.sub(short, text)
    return text


def _looks_like_word(tag: str) -> bool:
    """Écarte le charabia (pseudos, codes : « oielbmyrarq ») : trop de consonnes d'affilée,
    ou une lettre répétée 3 fois."""
    if not tag or tag.isdigit():
        return False
    letters = re.sub(r"\d", "", tag)
    if re.search(r"[bcdfghjklmnpqrstvwxz]{6,}", letters) or re.search(r"(.)\1\1", letters):
        return False
    vowels = sum(c in "aeiouy" for c in letters)
    return len(letters) < 6 or 0.25 <= vowels / len(letters) <= 0.7


def _tag(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(text.lower()))


def _good_word(w: str) -> bool:
    w = w.lower()
    if len(w) < 4 or w in STOP or w in VAGUE or w.isdigit():
        return False
    # adverbes, verbes conjugués, adjectifs génériques : rarement un bon hashtag
    if w.endswith(("ment", "ly", "ing", "ed", "ive", "ible", "able", "ous", "ary", "ful", "ait", "aient", "ez")):
        return False
    return not (w.endswith(("é", "ée", "és", "ées")) and not w.endswith(("té", "tés")))


def _combos(text: str) -> list[str]:
    """« GTA 6 », « GTA VI », « FIFA 25 », « iPhone 17 » -> gta6, fifa25, iphone17."""
    out = []
    pattern = r"\b([A-Za-z][A-Za-z]{1,12})[\s-]?(\d{1,2}|I{1,3}|IV|VI{0,3}|IX|X)\b"
    for m in re.finditer(pattern, text):
        name, num = m.group(1), m.group(2)
        if name.lower() in STOP or name.lower() in VAGUE:
            continue
        before = re.findall(r"[\w'’]+", text[: m.start()])
        if before and before[-1][:1].isupper() and _good_word(before[-1]):
            continue  # « Super Mario BROS 3 » : on ne coupe pas un nom en plusieurs morceaux
        roman = ROMAN.get(num.lower())
        if roman and not name.isupper():  # « je VI » n'est pas un nom de jeu
            continue
        out.append(_tag(name + (roman or num)))
    return out


def _words(text: str) -> list[str]:
    """Mots du texte, sans les élisions (« l'unité » -> « unité », « j'ai » -> « ai »)."""
    return [_ELISION.sub("", w) for w in re.findall(r"[\w'’-]+|[.!?:;,()+|⭐]", text)]


def _proper_phrases(text: str, from_title: bool = False) -> list[str]:
    """Noms propres : mots capitalisés. Dans une phrase normale, les mots capitalisés qui se
    suivent sont regroupés (« Vice City » -> vicecity) ; dans un titre où presque tout est en
    majuscules, chaque mot reste seul (sinon « Walkthrough Gameplay » deviendrait un nom)."""
    toks = _words(text)
    letters = [t for t in toks if t[:1].isalpha()]
    title_case = sum(t[:1].isupper() for t in letters) > 0.5 * max(1, len(letters))
    out, run, prev = [], [], "."
    for tok in toks + ["."]:
        word = tok.strip("'’-")
        cap = word[:1].isupper() and (from_title or prev not in ".!?:") and _good_word(word)
        if cap and not title_case:
            run.append(word)
        else:
            if run:
                out.append(_tag("".join(run[:3])))
            run = []
            if cap:
                out.append(_tag(word))
        prev = tok
    return [t for t in out if 4 <= len(t) <= 24]


def spoken_tags(text: str) -> set[str]:
    """Mots (et paires de mots) prononcés dans la vidéo, sous forme de hashtag."""
    toks = [_tag(w) for w in _words(_normalize(text))]
    toks = [t for t in toks if t]
    return set(toks) | {a + b for a, b in zip(toks, toks[1:])}


def topic_tags(title: str, limit: int = 3, spoken: set[str] | None = None) -> list[str]:
    """Hashtags du sujet de la vidéo, tirés de son titre (ex. gta6, rockstargames).

    Avec `spoken`, un mot du titre n'est gardé que s'il est aussi prononcé dans la vidéo
    (sauf « nom + numéro » comme gta6) : cela écarte pseudos, codes et mots de racolage.
    """
    if not title:
        return []
    title = _normalize(title)
    combos = _combos(title)
    others = _proper_phrases(title, from_title=True) + [_tag(w) for w in _words(title) if _good_word(w)]
    if spoken is not None:
        others = [t for t in others if t in spoken]
    tags = combos + others
    seen, out = set(), []
    for t in tags:
        # « gta » est inutile si « gta6 » est déjà là
        if not t or t in seen or not _looks_like_word(t) or any(t != o and o.startswith(t) for o in tags[:3]):
            continue
        if any(t in o or o in t for o in out):  # « criminels » déjà dans « reseauxcriminels »
            continue
        seen.add(t)
        out.append(t)
    return out[:limit]


def clip_hashtags(text: str, ranked: list[str], topic: list[str], used: Counter, total: int = 5) -> list[str]:
    """Hashtags d'un clip : sujet de la vidéo + ce qui est dit dans CE passage.

    Les mots déjà utilisés par d'autres clips de la même vidéo sont évités, pour que chaque
    clip ait ses propres hashtags.
    """
    text = _normalize(text)
    specific = _combos(text) + _proper_phrases(text)
    specific += [_tag(w) for w in ranked if _good_word(w)]
    out = list(topic[:2])
    for pass_ in (0, 1):  # 1er tour : seulement des mots pas encore pris par un autre clip
        for t in specific:
            if len(out) >= total:
                break
            if not t or t in out or not _looks_like_word(t) or any(t in o or o in t for o in out if len(o) > 3):
                continue
            if pass_ == 0 and used[t]:
                continue
            out.append(t)
    return out[:total]


def title_language(title: str) -> str:
    """Langue probable d'un titre court : 'fr', 'en' ou '' si on ne sait pas."""
    toks = _tokens(title)
    fr = sum(t in {"le", "la", "les", "je", "tu", "est", "pas", "que", "et", "des", "une", "du", "en",
                   "pour", "avec", "sur", "dans", "ce", "c", "j", "fini", "sans", "mon", "ma"} for t in toks)
    fr += sum(1 for ch in title.lower() if ch in "éèêàçùôî")
    en = sum(t in {"the", "and", "you", "is", "that", "to", "of", "it", "i", "a", "my", "with", "for",
                   "in", "on", "this", "how", "what", "why"} for t in toks)
    if fr == en:
        return ""
    return "fr" if fr > en else "en"


def hook_strength(text: str) -> float:
    t = text.strip()
    score = sum(w for rx, w in HOOK_RE if rx.search(t))
    if t.endswith("?"):
        score += 2.5
    if t.endswith("!"):
        score += 1
    if NUMBER.search(t):
        score += 1.5
    if YOU.search(t):
        score += 1
    if BAD_START.match(t):
        score -= 2.5
    if VAGUE_START.match(t):
        score -= 1.5
    n = len(t.split())
    if 4 <= n <= 14:  # une accroche courte se lit en un coup d'œil
        score += 1
    return score


def audio_energy(media: Path, step: float = 0.5) -> list[float] | None:
    """Volume (dB) par tranche de `step` secondes. None si pas d'audio / ffmpeg indisponible."""
    try:
        import numpy as np

        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", "8000",
             "-f", "s16le", "-"], capture_output=True, check=True, timeout=1800,
        ).stdout
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        hop = int(8000 * step)
        if len(x) < hop:
            return None
        frames = x[: len(x) // hop * hop].reshape(-1, hop)
        rms = np.sqrt((frames ** 2).mean(axis=1)) + 1e-6
        return list(20 * np.log10(rms / 32768))
    except Exception:
        return None


def _squash(x: float, mid: float, spread: float) -> float:
    """Ramène un score brut sur 0-100 (sigmoïde)."""
    return 100 / (1 + math.exp(-(x - mid) / spread))


def shorten(text: str, max_words: int) -> str:
    words = text.strip().split()
    if len(words) <= max_words:
        return text.strip()
    cut = words[:max_words]
    while cut and _tokens(cut[-1]) and _tokens(cut[-1])[0] in STOP:
        cut.pop()
    return " ".join(cut).rstrip(",;:") + "…"


# --------------------------------------------------------------------------- analyse

class _SegFeat:
    __slots__ = ("seg", "hook", "emotion", "numbers", "you", "laugh", "cta", "words", "energy",
                 "tokens", "complete")

    def __init__(self, seg: Segment, energy: float):
        t = seg.text
        toks = _tokens(t)
        self.seg = seg
        self.hook = hook_strength(t)
        self.emotion = sum(1 for w in toks if w in EMOTION)
        self.numbers = len(NUMBER.findall(t))
        self.you = len(YOU.findall(t))
        self.laugh = 1 if LAUGH.search(t) else 0
        self.cta = 1 if CTA.search(t) else 0
        self.words = len(seg.words) or len(toks)
        self.energy = energy
        self.tokens = [w for w in toks if w not in STOP and len(w) > 3 and not w.isdigit()]
        self.complete = t.rstrip().endswith((".", "!", "?", "…"))


def overlaps_any(start: float, end: float, zones, tolerance: float = 1.5) -> bool:
    """Vrai si [start, end] chevauche une des zones de plus de `tolerance` secondes."""
    return any(min(end, b) - max(start, a) > tolerance for a, b in zones or [])


def analyze(segments: list[Segment], *, num_clips: int, min_s: float, max_s: float,
            media: Path | None = None, title: str = "", exclude=None) -> list:
    from .highlights import Highlight

    segs = [s for s in segments if s.text.strip()]
    if not segs:
        return []
    lang = detect_language(segs)
    energy = audio_energy(media) if media else None

    def seg_energy(s: Segment) -> float:
        if not energy:
            return 0.0
        a, b = int(s.start / 0.5), max(int(s.start / 0.5) + 1, int(s.end / 0.5))
        chunk = energy[a:b] or [energy[-1]]
        return sum(chunk) / len(chunk)

    feats = [_SegFeat(s, seg_energy(s)) for s in segs]
    if energy:
        voiced = sorted(f.energy for f in feats)
        ref = voiced[len(voiced) // 2]
        for f in feats:
            f.energy -= ref  # énergie relative à la voix « normale » de la vidéo

    # rareté des mots dans la vidéo (TF-IDF) : un passage avec ses propres mots-clés
    # parle d'un sujet précis, il se comprend mieux seul
    df = Counter(w for f in feats for w in set(f.tokens))
    n_docs = len(feats)
    idf = {w: math.log((1 + n_docs) / (1 + c)) + 1 for w, c in df.items()}
    video_end = segs[-1].end

    candidates = []
    for i, first in enumerate(feats):
        start = first.seg.start
        for j in range(i, len(feats)):
            end = feats[j].seg.end
            dur = end - start
            if dur > max_s:
                break
            if dur < min_s:
                continue
            window = feats[i: j + 1]
            candidates.append(_score_window(window, start, end, idf, video_end, lang))

    if not candidates:  # vidéo plus courte que min_s : on prend tout
        window = feats
        candidates.append(_score_window(window, window[0].seg.start, window[-1].seg.end, idf,
                                        video_end, lang))

    if exclude:  # passages déjà montés : on cherche d'autres moments
        candidates = [c for c in candidates if not overlaps_any(c["start"], c["end"], exclude)]
    picked = sorted(select_best(candidates, num_clips), key=lambda c: -c["score"])
    topic = topic_tags(title, spoken=spoken_tags(" ".join(s.text for s in segs)))
    used: Counter = Counter()
    for c in picked:
        c["hashtags"] = clip_hashtags(c.pop("_text"), c.pop("_ranked"), topic, used)
        used.update(c["hashtags"])
    for c in candidates:  # libère la mémoire des autres candidats
        c.pop("_text", None)
        c.pop("_ranked", None)
    return [Highlight(**c) for c in picked]


def select_best(candidates: list[dict], k: int) -> list[dict]:
    """Choisit jusqu'à k passages qui ne se chevauchent pas, avec le meilleur score total.

    Contrairement à « prendre le meilleur, puis le suivant qui rentre », cette méthode
    (programmation dynamique sur les intervalles) laisse de la place pour les autres clips :
    on obtient bien le nombre demandé dès que la vidéo est assez longue.
    """
    import bisect

    if not candidates or k <= 0:
        return []
    cands = sorted(candidates, key=lambda c: c["end"])
    ends = [c["end"] for c in cands]
    n = len(cands)
    # prev[i] = nombre de candidats qui finissent avant le début du candidat i
    prev = [bisect.bisect_right(ends, c["start"]) for c in cands]
    neg = float("-inf")
    # best[j][i] : meilleur total avec exactement j clips parmi les i premiers candidats
    best = [[0.0] * (n + 1)] + [[neg] * (n + 1) for _ in range(k)]
    for j in range(1, k + 1):
        row, below = best[j], best[j - 1]
        for i in range(1, n + 1):
            take = below[prev[i - 1]]
            take = take + cands[i - 1]["score"] + 1.0 if take != neg else neg  # +1 : chaque clip compte
            row[i] = row[i - 1] if row[i - 1] >= take else take
    j = max((j for j in range(k, 0, -1) if best[j][n] != neg), default=0)
    picked, i = [], n
    while j > 0 and i > 0:
        if best[j][i] == best[j][i - 1]:
            i -= 1
            continue
        picked.append(cands[i - 1])
        i, j = prev[i - 1], j - 1
    return picked


def _score_window(window: list[_SegFeat], start: float, end: float, idf: dict, video_end: float,
                  lang: str) -> dict:
    dur = max(end - start, 1.0)
    first = window[0]
    opening = [f for f in window if f.seg.start < start + 4] or [first]

    # ---- accroche
    hook_raw = first.hook + 0.5 * max((f.hook for f in opening[1:]), default=0)
    hook_raw += 0.15 * max(0.0, max(f.energy for f in opening))
    hook_score = _squash(hook_raw, 2.0, 1.6)

    # ---- rétention
    words = sum(f.words for f in window)
    pace = words / dur  # mots par seconde ; ~2,5-3,5 = dynamique
    pace_s = -abs(pace - 3.0) * 1.2 + 1.0
    speech = sum(f.seg.end - f.seg.start for f in window) / dur  # part de temps parlé
    energies = [f.energy for f in window]
    mean_e = sum(energies) / len(energies)
    var_e = math.sqrt(sum((e - mean_e) ** 2 for e in energies) / len(energies))
    ending = 1.5 if window[-1].complete else -1.5
    payoff = max(f.hook for f in window[len(window) // 2:])  # une chute forte en fin de passage
    ret_raw = pace_s + 3 * (speech - 0.75) + 0.08 * mean_e + 0.1 * var_e + ending + 0.25 * payoff
    retention_score = _squash(ret_raw, 1.0, 1.4)

    # ---- partage
    emo = sum(f.emotion for f in window)
    nums = sum(f.numbers for f in window)
    you = sum(f.you for f in window)
    laugh = sum(f.laugh for f in window)
    toks = [w for f in window for w in f.tokens]
    tf = Counter(toks)
    distinct = sum(sorted((c * idf.get(w, 1) for w, c in tf.items()), reverse=True)[:6]) / 6
    share_raw = 0.6 * emo + 0.5 * min(nums, 4) + 0.35 * min(you, 6) + 1.5 * laugh + 0.5 * distinct
    share_score = _squash(share_raw, 4.0, 2.0)

    # ---- pénalités : pubs, intro/outro, passage qui démarre « au milieu »
    penalty = 12 * sum(f.cta for f in window)
    if start < 20:
        penalty += 8
    if video_end - end < 15:
        penalty += 6
    if BAD_START.match(first.seg.text.strip()):
        penalty += 6

    score = 0.42 * hook_score + 0.33 * retention_score + 0.25 * share_score - penalty
    score = max(0.0, min(100.0, score))

    # ---- textes
    ranked = sorted(tf.items(), key=lambda kv: -(kv[1] * idf.get(kv[0], 1) + 0.15 * len(kv[0])))
    keywords = [w for w, _ in ranked][:6]
    numbers = [m.group(0).strip() for f in window for m in NUMBER.finditer(f.seg.text)][:2]
    by_hook = sorted(window, key=lambda f: -f.hook)
    best_line = by_hook[0].seg.text.strip()
    opener = first.seg.text.strip()
    use_opener = first.hook >= 3 and len(opener.split()) <= 12
    hook_line = opener if use_opener else best_line
    hook_text = shorten(hook_line.rstrip("."), 9)
    # titre : une autre phrase forte du passage si possible, sinon la phrase d'accroche complète
    others = [f.seg.text.strip() for f in by_hook if f.seg.text.strip() != hook_line and f.hook >= 2]
    title_line = others[0] if others else hook_line
    low = hook_text.lower()
    if hook_text.endswith("?"):
        emoji = EMOJI["question"]
    elif re.search(r"argent|euro|million|riche|money|dollar|rich", low):
        emoji = EMOJI["money"]
    elif any(w in EMOTION for w in _tokens(low)):
        emoji = EMOJI["shock"]
    else:
        emoji = EMOJI["default"]

    title = shorten(title_line.rstrip("."), 12)
    title = title[:1].upper() + title[1:]
    cta = "Tu en penses quoi ? 👇" if lang == "fr" else "What do you think? 👇"
    description = f"{shorten(opener, 18)} {cta}"
    hashtags: list[str] = []  # fabriqués dans analyze(), une fois les clips choisis (titre + diversité)

    # ---- explication
    reasons = []
    if first.seg.text.strip().endswith("?"):
        reasons.append("démarre par une question" if lang == "fr" else "opens with a question")
    elif first.hook >= 3:
        reasons.append("première phrase percutante" if lang == "fr" else "punchy first line")
    if nums:
        reasons.append("chiffre marquant" if lang == "fr" else "striking number")
    if pace >= 2.6:
        reasons.append("débit rapide" if lang == "fr" else "fast pace")
    if var_e > 4 or mean_e > 3:
        reasons.append("voix énergique" if lang == "fr" else "energetic delivery")
    if you >= 2:
        reasons.append("parle directement au spectateur" if lang == "fr" else "talks to the viewer")
    if laugh:
        reasons.append("moment drôle" if lang == "fr" else "funny moment")
    if emo >= 2:
        reasons.append("forte émotion" if lang == "fr" else "strong emotion")
    if window[-1].complete and payoff >= 3:
        reasons.append("se termine sur une chute" if lang == "fr" else "ends on a payoff")
    if not reasons:
        reasons.append("passage fluide et complet" if lang == "fr" else "smooth, self-contained passage")
    reason = ", ".join(reasons[:3])
    reason = reason[:1].upper() + reason[1:] + "."

    return {
        "start": start, "end": end, "title": title, "hook": f"{hook_text} {emoji}",
        "description": description, "hashtags": hashtags[:6], "score": round(score, 1),
        "keywords": keywords + numbers, "hook_score": round(hook_score, 1),
        "retention_score": round(retention_score, 1), "share_score": round(share_score, 1),
        "reason": reason,
        "_text": " ".join(f.seg.text for f in window),
        "_ranked": [w for w, _ in ranked[:25]],
    }
