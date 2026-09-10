#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=pc-shell

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  JWT_KEY="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  STORAGE_KEY="$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  TS_DNS=""
  if command -v tailscale >/dev/null 2>&1; then
    TS_DNS="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
  fi
  BASE_URL="${TS_DNS:+https://$TS_DNS}"
  BASE_URL="${BASE_URL:-https://CHANGE-ME.ts.net}"
  cat > "$APP_DIR/.env" <<ENV
GH_CLIENT_ID=
GH_CLIENT_SECRET=
ALLOWED_GITHUB_USERS=
MCP_BASE_URL=$BASE_URL
MCP_JWT_SIGNING_KEY=$JWT_KEY
MCP_STORAGE_KEY=$STORAGE_KEY
ENV
  chmod 600 "$APP_DIR/.env"
fi

sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<UNIT
[Unit]
Description=pc-shell MCP server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/server.py
Restart=on-failure
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$HOME

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null

if command -v tailscale >/dev/null 2>&1; then
  echo "Configure the stable public endpoint once with: sudo tailscale funnel --bg 8000"
else
  echo "Install Tailscale, sign in, then run: sudo tailscale funnel --bg 8000"
fi

echo "Edit $APP_DIR/.env and set GH_CLIENT_ID, GH_CLIENT_SECRET, ALLOWED_GITHUB_USERS, and MCP_BASE_URL."
echo "GitHub OAuth callback: <MCP_BASE_URL>/auth/callback"
echo "Then start: sudo systemctl restart $SERVICE"
