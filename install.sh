#!/usr/bin/env bash
# One-command installer for pc-shell MCP server (Linux).
# Sets up a venv, generates a token, and installs a systemd service that
# auto-starts on boot and auto-restarts on crash.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${PC_SHELL_DIR:-$SCRIPT_DIR}"
SERVICE_NAME="pc-shell"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; }

# --- 1. dependencies -------------------------------------------------------- #
if ! command -v python3 >/dev/null 2>&1; then
  err "python3 not found. Install it first (e.g. sudo apt install python3 python3-venv)."
  exit 1
fi
if ! command -v openssl >/dev/null 2>&1; then
  err "openssl not found. Install it first (e.g. sudo apt install openssl)."
  exit 1
fi

# --- 2. virtualenv + deps --------------------------------------------------- #
say "Creating virtualenv in $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
say "Installing dependencies"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# --- 3. token / .env -------------------------------------------------------- #
if [ ! -f "$APP_DIR/.env" ]; then
  TOKEN="$(openssl rand -hex 32)"
  echo "MCP_TOKEN=$TOKEN" > "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  say "Generated a new MCP_TOKEN and wrote $APP_DIR/.env"
else
  say ".env already exists - keeping your existing MCP_TOKEN"
fi
TOKEN_VALUE="$(grep -E '^MCP_TOKEN=' "$APP_DIR/.env" | head -n1 | cut -d= -f2-)"

# --- 4. systemd service ----------------------------------------------------- #
if command -v systemctl >/dev/null 2>&1; then
  say "Installing systemd service (needs sudo)"
  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
  sed -e "s|__USER__|$USER|g" \
      -e "s|__APP_DIR__|$APP_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      "$APP_DIR/pc-shell.service" | sudo tee "$UNIT" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$SERVICE_NAME"
  say "Service '$SERVICE_NAME' is enabled and running."
  say "Logs:   journalctl -u $SERVICE_NAME -f"
  say "Status: systemctl status $SERVICE_NAME"
else
  err "systemctl not found; skipping service install. Run manually:"
  echo "  $APP_DIR/.venv/bin/python $APP_DIR/server.py"
fi

# --- 5. next steps ---------------------------------------------------------- #
cat <<EOF

------------------------------------------------------------
pc-shell MCP server is set up.

  Local endpoint : http://127.0.0.1:8000/mcp
  Bearer token   : $TOKEN_VALUE

Expose it with a FREE, persistent public URL (Tailscale Funnel):
  1. Install Tailscale + log in:   https://tailscale.com/download
  2. In admin console: enable MagicDNS + HTTPS certificates,
     and add the 'funnel' node attribute in Access Controls.
  3. Run (persists across reboots):
       tailscale funnel --bg 8000
  4. Your public MCP endpoint is:
       https://<machine>.<tailnet>.ts.net/mcp

Then add it in your MCP client with the bearer token above.
------------------------------------------------------------
EOF
