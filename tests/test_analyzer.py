from openclip.analyzer import analyze, detect_language, hook_strength, shorten
from openclip.highlights import find_highlights
from openclip.transcribe import Segment, Word


def seg(start, text, wps=3.0):
    words, t = [], start
    for w in text.split():
        words.append(Word(t, t + 0.8 / wps * 2, w))
        t += 1 / wps
    return Segment(start, t, text, words)


def build(sentences, gap=0.3):
    out, t = [], 0.0
    for s in sentences:
        out.append(seg(t, s))
        t = out[-1].end + gap
    return out


BLAND = "et donc ensuite on a regardé un peu les choses comme ça avec les gens du coin."
PUNCHY = [
    "Personne ne te dit ça mais le vrai secret pour gagner 10000 euros par mois ?",
    "Tu dois comprendre une erreur que tout le monde fait au début.",
    "J'ai perdu 3 ans à cause de ça et c'est vraiment incroyable.",
    "Si tu fais ça chaque jour ton business change complètement.",
    "La régularité bat le talent tous les jours, absolument tous les jours.",
    "Voici comment j'ai multiplié mes revenus par 5 en 12 mois.",
    "Et le plus fou, c'est que personne ne parle de cette méthode !",
]


def test_detect_language():
    assert detect_language(build(PUNCHY)) == "fr"
    assert detect_language(build(["You need to know the secret and it is not what you think."] * 5)) == "en"


def test_hook_strength_ranks_punchy_lines_higher():
    assert hook_strength(PUNCHY[0]) > hook_strength(BLAND) + 3


def test_shorten():
    assert shorten("un deux trois quatre cinq six", 3).endswith("…")
    assert shorten("court", 5) == "court"


def test_local_analysis_finds_the_punchy_passage():
    sentences = [BLAND] * 25 + PUNCHY * 2 + [BLAND] * 25
    segs = build(sentences)
    clips = analyze(segs, num_clips=3, min_s=30, max_s=60)
    assert clips
    best = clips[0]
    punchy_start = segs[25].start
    punchy_end = segs[25 + len(PUNCHY) * 2 - 1].end
    assert best.start >= punchy_start - 5 and best.end <= punchy_end + 5
    assert best.score > max(c.score for c in clips[1:]) if len(clips) > 1 else True
    for c in clips:
        assert 30 <= c.end - c.start <= 60
        assert c.title and c.hook and c.reason and c.hashtags and c.keywords
        assert 0 <= c.score <= 100
    starts = sorted((c.start, c.end) for c in clips)
    assert all(a[1] <= b[0] for a, b in zip(starts, starts[1:]))


def test_find_highlights_without_key_uses_local(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
    segs = build([BLAND] * 10 + PUNCHY * 2 + [BLAND] * 10)
    clips = find_highlights(segs, title="t", num_clips=2, min_s=30, max_s=60)
    assert clips and clips[0].reason


def test_select_best_fits_requested_count():
    from openclip.analyzer import select_best
    # glouton : prendrait [20-70] (score 100) et bloquerait les deux autres
    cands = [{"start": 20, "end": 70, "score": 100}, {"start": 0, "end": 40, "score": 90},
             {"start": 45, "end": 90, "score": 90}]
    picked = select_best(cands, 2)
    assert sorted((c["start"], c["end"]) for c in picked) == [(0, 40), (45, 90)]
    assert [c["score"] for c in select_best(cands, 1)] == [100]
    assert select_best([], 3) == []


def test_requested_count_on_long_enough_video():
    import random
    random.seed(3)
    segs = build([random.choice([BLAND] + PUNCHY) for _ in range(160)])  # ~12 min
    clips = find_highlights(segs, title="t", num_clips=12, min_s=30, max_s=60)
    assert len(clips) == 12


def test_hashtags_follow_video_and_clip():
    from openclip.analyzer import topic_tags
    assert topic_tags("Grand Theft Auto 6 | Official Pre-Order Trailer")[0] == "gta6"
    assert topic_tags("GTA VI Walkthrough Gameplay Part 1")[0] == "gta6"
    parts = [
        "Lucia and Jason drive through Vice City in a Porsche and the airboat in Leonida is wild.",
        "Personne ne te dit ça mais le secret pour gagner 10000 euros ?",
        "The police chase near the Keys with helicopters everywhere is crazy.",
        "Tu dois comprendre une erreur que tout le monde fait au début.",
    ]
    segs = build([BLAND] * 4 + parts * 6 + [BLAND] * 4 + [p.replace("Vice City", "Miami") for p in parts] * 6 + [BLAND] * 4)
    clips = analyze(segs, num_clips=3, min_s=30, max_s=60, title="GTA 6 Review")
    tags = [c.hashtags for c in clips]
    assert all(t[0] == "gta6" for t in tags)
    assert all("fyp" not in t and "shorts" not in t and "pourtoi" not in t for t in tags)
    assert len({tuple(t[1:]) for t in tags}) == len(tags)  # chaque clip a ses propres hashtags


def test_hashtags_skip_gibberish_and_filler():
    from collections import Counter

    from openclip.analyzer import clip_hashtags
    tags = clip_hashtags("please stay guys, the heist in Vice City with Lucia", ["please", "stay", "guys", "heist"],
                         ["gta6"], Counter())
    assert "please" not in tags and "guys" not in tags and "vicecity" in tags
    from openclip.analyzer import spoken_tags, topic_tags
    # mot du titre jamais prononcé (pseudo, code) : écarté ; « nom + numéro » toujours gardé
    sp = spoken_tags("rockstar did it again in vice city")
    assert topic_tags("GTA 6 oielbmyrarq ROCKSTAR", spoken=sp) == ["gta6", "rockstar"]
