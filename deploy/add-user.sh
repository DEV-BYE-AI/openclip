#!/usr/bin/env bash
# Crée un OpenClip séparé pour une autre personne, sur le même serveur.
# Tout est isolé : projets, clips, comptes TikTok/YouTube, clés, mode auto, mot de passe.
#
#   sudo bash deploy/add-user.sh cousin
#        -> https://cousin.<ip>.sslip.io   (si Caddy est installé, sinon http://<ip>:<port>)
#
# Options : --port 8002   --domain cousin.mondomaine.fr
# Supprimer : sudo bash deploy/add-user.sh cousin --remove
set -euo pipefail

NAME="${1:-}"
shift || true
PORT=""
DOMAIN=""
REMOVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --remove) REMOVE=1; shift ;;
    *) echo "Option inconnue : $1"; exit 1 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then echo "Lance ce script avec sudo."; exit 1; fi
if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{1,20}$ ]]; then
  echo "Donne un nom en minuscules, sans espace (ex. : sudo bash deploy/add-user.sh cousin)"; exit 1
fi
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
SERVICE="openclip-$NAME"
DATA="$RUN_HOME/openclip-users/$NAME"
CADDYFILE=/etc/caddy/Caddyfile
say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }

if [[ $REMOVE -eq 1 ]]; then
  say "Suppression de l'espace « $NAME »"
  systemctl disable --now "$SERVICE" 2>/dev/null || true
  rm -f "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
  if [[ -f "$CADDYFILE" ]] && grep -q "# >>> openclip-$NAME" "$CADDYFILE"; then
    cp "$CADDYFILE" "$CADDYFILE.avant-suppression-$NAME.$(date +%s)"
    sed -i "/^# >>> openclip-$NAME\$/,/^# <<< openclip-$NAME\$/d" "$CADDYFILE"
    systemctl reload caddy || true
  fi
  echo "Service supprimé. Ses données sont toujours dans $DATA (supprime ce dossier pour tout effacer)."
  exit 0
fi

# port libre (8002, 8003…) ; ports internes du navigateur décalés pour ne pas gêner les autres
if [[ -z "$PORT" ]]; then
  if [[ -f "/etc/systemd/system/$SERVICE.service" ]]; then
    PORT="$(grep -oP -- '--port \K[0-9]+' "/etc/systemd/system/$SERVICE.service" || true)"
  fi
  PORT="${PORT:-8002}"
  while ss -ltn | awk '{print $4}' | grep -qE "[:.]${PORT}\$" && ! systemctl is-active --quiet "$SERVICE"; do
    PORT=$((PORT + 1))
  done
fi
OFFSET=$((PORT - 8000))

say "Création de l'espace « $NAME » (port $PORT)"
sudo -u "$RUN_USER" mkdir -p "$DATA/home" "$DATA/output"
ENV_FILE="$DATA/env"
if [[ ! -f "$ENV_FILE" ]]; then
  PASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
  cat > "$ENV_FILE" <<ENV
OPENCLIP_USER=$NAME
OPENCLIP_PASSWORD=$PASS
OPENCLIP_HOME=$DATA/home
OPENCLIP_ENV_FILE=$ENV_FILE
OPENCLIP_DISPLAY=:$((77 + OFFSET))
OPENCLIP_VNC_PORT=$((5977 + OFFSET))
OPENCLIP_CDP_PORT=$((9223 + OFFSET))
WHISPER_MODEL=base
ENV
  NEW_PASS="$PASS"
fi
chown "$RUN_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"

# adresse : sous-domaine sslip.io automatique si Caddy est là
if [[ -z "$DOMAIN" ]] && command -v caddy >/dev/null && systemctl is-active --quiet caddy; then
  IP="$(curl -s -4 --max-time 5 https://ifconfig.me || hostname -I | awk '{print $1}')"
  DOMAIN="$NAME.${IP//./-}.sslip.io"
fi
if [[ -n "$DOMAIN" ]]; then HOST=127.0.0.1; else HOST=0.0.0.0; fi

cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=OpenClip ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DATA
EnvironmentFile=$ENV_FILE
Environment=PATH=$APP_DIR/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$APP_DIR/.venv/bin/openclip web --host $HOST --port $PORT -o $DATA/output
Restart=on-failure
RestartSec=5
Nice=12

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

if [[ -n "$DOMAIN" ]]; then
  say "Adresse https : $DOMAIN (Caddy)"
  BACKUP="$CADDYFILE.avant-openclip-$NAME.$(date +%s)"
  cp "$CADDYFILE" "$BACKUP"
  sed -i "/^# >>> openclip-$NAME\$/,/^# <<< openclip-$NAME\$/d" "$CADDYFILE"
  cat >> "$CADDYFILE" <<CADDY

# >>> openclip-$NAME
$DOMAIN {
	reverse_proxy 127.0.0.1:$PORT {
		flush_interval -1
	}
}
# <<< openclip-$NAME
CADDY
  if caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    systemctl reload caddy
  else
    echo "Configuration Caddy invalide : je remets l'ancienne."
    cp "$BACKUP" "$CADDYFILE"; exit 1
  fi
  URL="https://$DOMAIN"
else
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "$PORT/tcp" >/dev/null; fi
  IP="$(curl -s -4 --max-time 5 https://ifconfig.me || hostname -I | awk '{print $1}')"
  URL="http://$IP:$PORT"
fi

sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
  echo "Le service ne démarre pas : sudo journalctl -u $SERVICE -n 50"; exit 1
fi
say "Espace « $NAME » en ligne : $URL"
echo "Identifiant : $NAME"
if [[ -n "${NEW_PASS:-}" ]]; then
  echo "Mot de passe : $NEW_PASS   (à donner à $NAME ; il est aussi dans $ENV_FILE)"
else
  echo "Mot de passe : inchangé (dans $ENV_FILE)"
fi
echo
echo "Ses projets, comptes et clés sont dans $DATA, séparés des tiens."
echo "Mise à jour de tous les OpenClip : cd $APP_DIR && git pull && sudo systemctl restart 'openclip*'"
