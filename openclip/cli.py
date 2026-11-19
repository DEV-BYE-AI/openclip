"""Interface en ligne de commande : `openclip --help`."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Settings


def _common_clip_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-n", "--num", type=int, default=5, help="nombre de clips par vidéo (déf. 5)")
    p.add_argument("--min", type=float, default=30, help="durée min en secondes (déf. 30)")
    p.add_argument("--max", type=float, default=60, help="durée max en secondes (déf. 60)")
    p.add_argument("--crop", choices=["face", "center", "blur"], default="face",
                   help="face = suit le visage, center = centre, blur = vidéo entière sur fond flou")
    p.add_argument("--no-subs", action="store_true", help="pas de sous-titres incrustés")
    p.add_argument("--lang", default=None, help="langue de la vidéo (fr, en…), auto par défaut")
    p.add_argument("-o", "--out", type=Path, default=Path("output"))
    _publish_args(p, with_upload=True)


def _publish_args(p: argparse.ArgumentParser, with_upload: bool = False) -> None:
    flag = "--upload" if with_upload else "--to"
    p.add_argument(flag, dest="targets", default="" if with_upload else "tiktok",
                   help="cibles séparées par des virgules : tiktok,youtube")
    p.add_argument("--tiktok-mode", choices=["direct", "draft"], default="direct",
                   help="direct = publication auto, draft = brouillon dans l'app TikTok")
    p.add_argument("--privacy", default=None,
                   help="TikTok : PUBLIC_TO_EVERYONE, SELF_ONLY… / YouTube : public, unlisted, private")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openclip", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="connecter un compte")
    a.add_argument("platform", choices=["tiktok", "youtube"])

    c = sub.add_parser("clip", help="créer des clips depuis un ou plusieurs liens YouTube")
    c.add_argument("urls", nargs="+")
    _common_clip_args(c)

    b = sub.add_parser("batch", help="traiter un fichier de liens (un par ligne)")
    b.add_argument("file", type=Path)
    _common_clip_args(b)

    u = sub.add_parser("upload", help="publier des clips déjà générés")
    u.add_argument("clip_dir", type=Path, help="ex. output/<id_video>")
    u.add_argument("--only", default="", help="numéros de clips, ex. 1,3")
    _publish_args(u)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    settings = Settings()

    if args.cmd == "auth":
        if args.platform == "tiktok":
            from .uploaders import tiktok
            tiktok.login(settings)
            info = tiktok.creator_info(settings)
            print(f"✅ TikTok connecté : @{info.get('creator_username', '?')}")
        else:
            from .uploaders import youtube
            youtube.login(settings)
            print("✅ YouTube connecté")
        return 0

    from .pipeline import make_clips, publish

    targets = [t.strip() for t in args.targets.split(",") if t.strip()]

    if args.cmd == "upload":
        only = [int(x) for x in args.only.split(",") if x.strip()] or None
        publish(args.clip_dir, settings, targets, tiktok_mode=args.tiktok_mode,
                privacy=args.privacy, only=only)
        return 0

    cs = settings.clip
    cs.num_clips, cs.min_seconds, cs.max_seconds = args.num, args.min, args.max
    cs.crop_mode, cs.subtitles, cs.language = args.crop, not args.no_subs, args.lang

    urls = args.urls if args.cmd == "clip" else [
        line.strip() for line in args.file.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    failures = 0
    for url in urls:
        try:
            clip_dir = make_clips(url, settings, args.out)
            if targets:
                publish(clip_dir, settings, targets, tiktok_mode=args.tiktok_mode,
                        privacy=args.privacy)
        except Exception as exc:
            failures += 1
            logging.exception("échec pour %s : %s", url, exc)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
