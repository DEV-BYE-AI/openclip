# OpenClip

Colle un lien YouTube et OpenClip produit automatiquement des **clips verticaux de 30 à 60 s**,
sous-titrés façon TikTok, puis peut les **publier sur TikTok et YouTube Shorts**.

```
lien YouTube ─► yt-dlp ─► faster-whisper ─► Claude (meilleurs moments) ─► ffmpeg 9:16 + sous-titres ─► TikTok / Shorts
```

## Fonctionnalités

- Téléchargement de la vidéo (yt-dlp, jusqu'en 1080p)
- Transcription locale au mot près (faster-whisper, langue détectée automatiquement)
- Choix des passages les plus viraux par Claude : accroche, titre, description, hashtags, score.
  Sans clé API, un algorithme de repli prend le relais.
- Coupe propre sur les fins de phrases, durée forcée entre `--min` et `--max`
- Recadrage 9:16 : `face` (suit le visage), `center`, ou `blur` (vidéo entière sur fond flou)
- Sous-titres animés (3 mots à l'écran, mot prononcé surligné en jaune) et accroche affichée pendant 3 s
- Publication TikTok via l'API officielle **Content Posting** (post direct ou brouillon)
- Publication YouTube Shorts via l'API YouTube Data v3
- Pas de double publication : chaque envoi est enregistré dans `clips.json`

## Installation

Prérequis : Python 3.10+ et `ffmpeg` (`brew install ffmpeg` / `sudo apt install ffmpeg`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[face]"
cp .env.example .env   # puis remplis les clés
```

## Utilisation

```bash
# 1. Créer des clips (dans output/<id_video>/)
openclip clip "https://www.youtube.com/watch?v=XXXX" -n 5

# 2. Tout en un : clips + publication TikTok et YouTube Shorts
openclip clip "https://youtu.be/XXXX" --upload tiktok,youtube

# Plusieurs liens dans un fichier (un par ligne, # pour commenter)
openclip batch liens.txt --upload tiktok

# Publier plus tard des clips déjà générés (ex. uniquement les clips 1 et 3)
openclip upload output/XXXX --to tiktok --only 1,3

# Options utiles
#   --min 30 --max 60       durée des clips
#   --crop face|center|blur recadrage
#   --lang fr               forcer la langue
#   --no-subs               sans sous-titres
#   --tiktok-mode draft     envoie en brouillon dans l'app TikTok au lieu de publier
#   --privacy SELF_ONLY     confidentialité (YouTube : public|unlisted|private)
```

Chaque dossier `output/<id_video>/` contient les `clip_XX.mp4`, les sous-titres `.ass`
et `clips.json` (titres, descriptions, hashtags, liens de publication). Tu peux modifier les
titres dans `clips.json` avant de lancer `openclip upload`.

## Connecter les comptes

### Claude (choix des moments)
Crée une clé sur <https://console.anthropic.com> et mets-la dans `ANTHROPIC_API_KEY`.
Le modèle utilisé est `claude-opus-5-5` (modifiable via `OPENCLIP_MODEL`).

### TikTok
1. Crée une app sur <https://developers.tiktok.com>, ajoute les produits **Login Kit** et
   **Content Posting API** (active « Direct Post »), scopes `user.info.basic`, `video.upload`, `video.publish`.
2. Déclare la redirect URI `http://localhost:8765/callback` (ou une autre, à reporter dans `.env`).
3. Renseigne `TIKTOK_CLIENT_KEY` et `TIKTOK_CLIENT_SECRET`, puis : `openclip auth tiktok`

> ⚠️ Tant que TikTok n'a pas **audité** ton app, les posts directs sont forcés en **privé**
> (visibles par toi seul). Pour publier en public, demande l'audit dans le portail développeur,
> ou utilise `--tiktok-mode draft` et valide chaque vidéo dans l'app.
> Si TikTok refuse une redirect URI `localhost`, mets une URL https à toi : OpenClip te demandera
> alors de coller l'URL de redirection dans le terminal.

### YouTube Shorts
1. Sur <https://console.cloud.google.com> : crée un projet, active **YouTube Data API v3**,
   crée des identifiants OAuth de type **Application de bureau**, télécharge le JSON en `client_secret.json`.
2. `openclip auth youtube`

> Les projets Google non vérifiés publient en privé et le quota par défaut (10 000 unités/jour)
> permet environ 6 uploads par jour.

Les jetons sont stockés dans `~/.openclip/tokens/` (permissions 600).

## Automatisation

Pour que ça tourne tout seul, ajoute tes liens dans `liens.txt` et programme une tâche cron :

```cron
0 */6 * * * cd /chemin/openclip && .venv/bin/openclip batch liens.txt --upload tiktok,youtube
```

Les vidéos déjà traitées réutilisent leur transcription en cache et les clips déjà publiés sont ignorés.

## Droits d'auteur

Ne publie que des clips de vidéos dont tu détiens les droits ou pour lesquelles tu as l'autorisation
du créateur. OpenClip ajoute automatiquement un crédit vers la vidéo source dans la description.

## Tests

```bash
pip install -e ".[dev]" && pytest
```
