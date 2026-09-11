#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OS="$(uname -s)"

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

config_ready=true
for key in GH_CLIENT_ID GH_CLIENT_SECRET ALLOWED_GITHUB_USERS MCP_BASE_URL MCP_JWT_SIGNING_KEY MCP_STORAGE_KEY; do
  value="$(grep -E "^${key}=" "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  if [[ -z "$value" || "$value" == *CHANGE-ME* ]]; then
    config_ready=false
  fi
done

install_macos() {
  local label="com.satianurag.pc-shell-mcp"
  local agents="$HOME/Library/LaunchAgents"
  local logs="$HOME/Library/Logs"
  local plist="$agents/$label.plist"
  mkdir -p "$agents" "$logs"

  APP_DIR="$APP_DIR" HOME_DIR="$HOME" PLIST="$plist" python3 - <<'PY'
import os
import plistlib

app = os.environ["APP_DIR"]
home = os.environ["HOME_DIR"]
plist = os.environ["PLIST"]
command = f'set -a; . "{app}/.env"; set +a; exec "{app}/.venv/bin/python" "{app}/server.py"'
data = {
    "Label": "com.satianurag.pc-shell-mcp",
    "ProgramArguments": ["/bin/sh", "-c", command],
    "WorkingDirectory": app,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "StandardOutPath": f"{home}/Library/Logs/pc-shell-mcp.log",
    "StandardErrorPath": f"{home}/Library/Logs/pc-shell-mcp.err.log",
}
with open(plist, "wb") as f:
    plistlib.dump(data, f)
PY
  chmod 600 "$plist"

  if [[ "$config_ready" == true ]]; then
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$plist"
    launchctl enable "gui/$(id -u)/$label"
    launchctl kickstart -k "gui/$(id -u)/$label"
    echo "pc-shell is running as macOS LaunchAgent: $label"
    echo "Logs: $logs/pc-shell-mcp.err.log"
  else
    echo "LaunchAgent installed but not started because .env is incomplete."
    echo "Fill .env, then rerun ./install.sh"
  fi
}

install_linux() {
  local service="pc-shell"
  command -v systemctl >/dev/null || { echo "systemctl is required on Linux" >&2; exit 1; }
  sudo tee "/etc/systemd/system/$service.service" >/dev/null <<UNIT
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
  sudo systemctl enable "$service" >/dev/null
  if [[ "$config_ready" == true ]]; then
    sudo systemctl restart "$service"
    echo "pc-shell is running as systemd service: $service"
  else
    echo "systemd service installed but not started because .env is incomplete."
    echo "Fill .env, then rerun ./install.sh"
  fi
}

case "$OS" in
  Darwin) install_macos ;;
  Linux) install_linux ;;
  *) echo "Unsupported OS: $OS (macOS and Linux are supported)" >&2; exit 1 ;;
esac

if command -v tailscale >/dev/null 2>&1; then
  echo "Stable public endpoint: run once if needed: tailscale funnel --bg 8000"
fi
echo "MCP URL: <MCP_BASE_URL>/mcp"
echo "GitHub OAuth callback: <MCP_BASE_URL>/auth/callback"
