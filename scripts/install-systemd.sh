#!/usr/bin/env bash
# Instala Ventanilla como servicios systemd de USUARIO:
#   gobchat-mock.service  servicio de documentos simulado en 127.0.0.1:8701
#   gobchat.service       chat en <IP de Tailscale>:8700 (solo accesible por la VPN)
#
# Uso:
#   ./scripts/install-systemd.sh                 # IP de Tailscale detectada, puerto 8700
#   GOBCHAT_HOST=100.x.y.z GOBCHAT_PORT=8700 ./scripts/install-systemd.sh
# Después:
#   systemctl --user enable --now gobchat-mock gobchat
#   journalctl --user -u gobchat -f
# Para que sigan corriendo sin sesión abierta: loginctl enable-linger "$USER"
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${GOBCHAT_HOST:-$(tailscale ip -4 2>/dev/null | head -1)}"
PORT="${GOBCHAT_PORT:-8700}"
MOCK_PORT="${GOBCHAT_MOCK_PORT:-8701}"
if [ -z "$HOST" ]; then
  echo "No se pudo determinar la IP de Tailscale; define GOBCHAT_HOST." >&2
  exit 1
fi
if [ "$HOST" = "0.0.0.0" ] || [ "$HOST" = "::" ]; then
  echo "⚠  $HOST expone el chat en TODAS las interfaces (incluida la LAN)." >&2
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/gobchat-mock.service" <<EOF
[Unit]
Description=Ventanilla - servicio de documentos simulado (MVP)
After=network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PYTHONWARNINGS=ignore
ExecStart=$ROOT/.venv/bin/python -m uvicorn mock_doc_service.main:app --host 127.0.0.1 --port $MOCK_PORT --log-level warning
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/gobchat.service" <<EOF
[Unit]
Description=Ventanilla - chat de tramites y leyes (MVP) en http://$HOST:$PORT
Wants=gobchat-mock.service
After=gobchat-mock.service network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PYTHONWARNINGS=ignore
# Si Tailscale aún no levanta la IP, el bind falla y systemd reintenta.
ExecStart=$ROOT/.venv/bin/python -m uvicorn app.main:app --host $HOST --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
echo "Unidades instaladas en $UNIT_DIR"
echo "Chat: http://$HOST:$PORT  (mock: http://127.0.0.1:$MOCK_PORT)"
echo "Activar: systemctl --user enable --now gobchat-mock gobchat"
