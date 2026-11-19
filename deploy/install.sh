#!/usr/bin/env bash
# Installe OpenClip sur un serveur Ubuntu/Debian, à côté d'un site existant, sans le modifier.
#
#   Mode test (le plus simple)     : sudo bash deploy/install.sh
#        -> http://IP_DU_SERVEUR:8001
#   Mode sous-domaine (https)      : sudo bash deploy/install.sh --domain clip.mondomaine.fr
#        -> https://clip.mondomaine.fr  (le DNS du sous-domaine doit pointer vers le serveur)
#        Fonctionne avec Caddy (https automatique) ou nginx (+ certbot).
#
# Options : --port 8001   --domain <sous-domaine>
set -euo pipefail

PORT=8001
DOMAIN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    *) echo "Option inconnue : $1"; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then echo "Lance ce script avec sudo."; exit 1; fi
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }

say "Vérification du port $PORT"
if ss -ltn | awk '{print $4}' | grep -qE "[:.]${PORT}\$"; then
  if systemctl is-active --quiet openclip; then
    echo "Le port $PORT est utilisé par OpenClip lui-même : mise à jour."
  else
    echo "Le port $PORT est déjà pris (peut-être par ton autre site). Relance avec --port 8002 par exemple."
    exit 1
  fi
fi

say "Installation des paquets système (ffmpeg, python)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg python3-venv python3-pip git curl >/dev/null

say "Installation du navigateur pour la connexion YouTube (écran virtuel + Chrome)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xvfb x11vnc novnc fonts-noto-color-emoji fonts-montserrat >/dev/null \
  || echo "Paquets écran virtuel non installés : la connexion YouTube depuis l'interface sera indisponible."
if ! command -v google-chrome-stable >/dev/null; then
  if curl -fsSL -o /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /tmp/google-chrome.deb >/dev/null \
      || echo "Chrome non installé : la connexion YouTube depuis l'interface sera indisponible."
    rm -f /tmp/google-chrome.deb
  else
    echo "Téléchargement de Chrome impossible : la connexion YouTube depuis l'interface sera indisponible."
  fi
fi

say "Installation d'OpenClip dans $APP_DIR/.venv"
sudo -u "$RUN_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
sudo -u "$RUN_USER" "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[face]"

ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  sudo -u "$RUN_USER" cp "$APP_DIR/.env.example" "$ENV_FILE"
fi
if ! grep -q '^OPENCLIP_PASSWORD=.\+' "$ENV_FILE"; then
  PASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
  sed -i '/^OPENCLIP_PASSWORD=/d;/^OPENCLIP_USER=/d' "$ENV_FILE"
  printf '\n# Accès à l interface web\nOPENCLIP_USER=admin\nOPENCLIP_PASSWORD=%s\n' "$PASS" >> "$ENV_FILE"
  NEW_PASS="$PASS"
fi
chmod 600 "$ENV_FILE"; chown "$RUN_USER" "$ENV_FILE"

if [[ -n "$DOMAIN" ]]; then HOST=127.0.0.1; else HOST=0.0.0.0; fi

say "Création du service systemd « openclip »"
cat > /etc/systemd/system/openclip.service <<UNIT
[Unit]
Description=OpenClip (interface web)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$APP_DIR/.venv/bin/openclip web --host $HOST --port $PORT -o $APP_DIR/output
Restart=on-failure
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now openclip
systemctl restart openclip

if [[ -n "$DOMAIN" ]]; then
  CADDYFILE=/etc/caddy/Caddyfile
  if command -v caddy >/dev/null && systemctl is-active --quiet caddy && [[ -f "$CADDYFILE" ]]; then
    say "Ajout de $DOMAIN dans Caddy (https automatique, ton site existant n'est pas modifié)"
    BACKUP="$CADDYFILE.avant-openclip.$(date +%s)"
    cp "$CADDYFILE" "$BACKUP"
    # retire un éventuel ancien bloc OpenClip puis ajoute le nouveau
    sed -i '/^# >>> openclip/,/^# <<< openclip/d' "$CADDYFILE"
    cat >> "$CADDYFILE" <<CADDY

# >>> openclip (ajouté par deploy/install.sh)
$DOMAIN {
	reverse_proxy 127.0.0.1:$PORT {
		flush_interval -1
	}
}
# <<< openclip
CADDY
    if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
      systemctl reload caddy
      echo "Caddy rechargé. Sauvegarde de l'ancienne config : $BACKUP"
    else
      echo "Configuration Caddy invalide : je remets l'ancienne pour ne rien casser."
      cp "$BACKUP" "$CADDYFILE"
      caddy validate --config "$CADDYFILE" --adapter caddyfile || true
      exit 1
    fi
  elif ! command -v nginx >/dev/null; then
    echo "Ni Caddy ni nginx détectés : configure ton serveur web pour rediriger $DOMAIN vers http://127.0.0.1:$PORT"
    echo "(voir deploy/nginx-openclip.conf)."
  else
    say "Ajout du site nginx $DOMAIN (ton site existant n'est pas modifié)"
    SITE=/etc/nginx/sites-available/openclip
    sed -e "s/clip.exemple.fr/$DOMAIN/g" -e "s/127.0.0.1:8001/127.0.0.1:$PORT/g" \
      "$APP_DIR/deploy/nginx-openclip.conf" > "$SITE"
    ln -sf "$SITE" /etc/nginx/sites-enabled/openclip
    if nginx -t 2>/dev/null; then
      systemctl reload nginx
    else
      echo "Configuration nginx invalide : je retire le site OpenClip pour ne rien casser."
      rm -f /etc/nginx/sites-enabled/openclip "$SITE"
      nginx -t
      exit 1
    fi
    if command -v certbot >/dev/null; then
      say "Certificat https (Let's Encrypt)"
      certbot --nginx -d "$DOMAIN" --redirect || echo "certbot a échoué : vérifie que le DNS de $DOMAIN pointe bien vers ce serveur."
    else
      echo "Pour le https : sudo apt install certbot python3-certbot-nginx && sudo certbot --nginx -d $DOMAIN"
    fi
  fi
  URL="https://$DOMAIN"
else
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    say "Ouverture du port $PORT dans le pare-feu (ufw)"
    ufw allow "$PORT/tcp" >/dev/null
  fi
  IP="$(curl -s -4 --max-time 5 https://ifconfig.me || hostname -I | awk '{print $1}')"
  URL="http://$IP:$PORT"
fi

sleep 2
if systemctl is-active --quiet openclip; then
  say "OpenClip est en ligne : $URL"
else
  echo "Le service ne démarre pas. Regarde : sudo journalctl -u openclip -n 50"
  exit 1
fi
echo "Identifiant : $(grep '^OPENCLIP_USER=' "$ENV_FILE" | cut -d= -f2)"
if [[ -n "${NEW_PASS:-}" ]]; then
  echo "Mot de passe : $NEW_PASS   (note-le, il est aussi dans $ENV_FILE)"
else
  echo "Mot de passe : celui de OPENCLIP_PASSWORD dans $ENV_FILE"
fi
echo
echo "Optionnel dans $ENV_FILE : ANTHROPIC_API_KEY (Claude), TikTok / YouTube, puis : sudo systemctl restart openclip"
echo "Si ton hébergeur a un pare-feu dans son panneau (OVH, Hetzner, Oracle…), ouvre aussi le port $PORT là-bas."
