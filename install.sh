#!/usr/bin/env bash
# One-command installer for pc-shell MCP server (Linux).
# Creates a venv, scaffolds .env (with generated secrets), and installs a systemd
# service that auto-starts on boot and auto-restarts on crash.
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

# --- 3. .env scaffold ------------------------------------------------------- #
if [ ! -f "$APP_DIR/.env" ]; then
  TOKEN="$(openssl rand -hex 32)"
  JWT_KEY="$("$APP_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  cat > "$APP_DIR/.env" <<ENV
# Fill in the GitHub OAuth values, your allow-list, and the real MCP_BASE_URL,
# then start the service. See .env.example for all options.
GH_CLIENT_ID=
GH_CLIENT_SECRET=
ALLOWED_GITHUB_USERS=
MCP_BASE_URL=https://CHANGE-ME.example.ts.net
MCP_JWT_SIGNING_KEY=$JWT_KEY
MCP_TOKEN=$TOKEN
ENV
  chmod 600 "$APP_DIR/.env"
  say "Wrote $APP_DIR/.env with a generated MCP_TOKEN and MCP_JWT_SIGNING_KEY"
else
  say ".env already exists - leaving it untouched"
fi

# --- 4. systemd service ----------------------------------------------------- #
if command -v systemctl >/dev/null 2>&1; then
  say "Installing systemd unit (needs sudo)"
  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
  sed -e "s|__USER__|$USER|g" \
      -e "s|__APP_DIR__|$APP_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      "$APP_DIR/pc-shell.service" | sudo tee "$UNIT" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
  say "Service enabled to start on boot."
else
  err "systemctl not found; skipping service install. Run manually after editing .env:"
  echo "  $APP_DIR/.venv/bin/python $APP_DIR/server.py"
fi

# --- 5. readiness check + next steps --------------------------------------- #
missing=""
for k in GH_CLIENT_ID GH_CLIENT_SECRET ALLOWED_GITHUB_USERS; do
  v="$(grep -E "^$k=" "$APP_DIR/.env" | head -n1 | cut -d= -f2-)"
  [ -z "$v" ] && missing="$missing $k"
done

cat <<EOF

------------------------------------------------------------
pc-shell MCP server is installed.

  Local endpoint : http://127.0.0.1:8000/mcp

Before starting, create a GitHub OAuth App:
  GitHub > Settings > Developer settings > OAuth Apps > New OAuth App
    Homepage URL:               <your MCP_BASE_URL>
    Authorization callback URL: <your MCP_BASE_URL>/auth/callback
  Then put the Client ID / secret, your GitHub username(s), and the
  real MCP_BASE_URL into: $APP_DIR/.env

Expose it with a FREE, persistent public URL (Tailscale Funnel):
  1. Install Tailscale + log in:   https://tailscale.com/download
  2. In admin console: enable MagicDNS + HTTPS certificates,
     and add the 'funnel' node attribute in Access Controls.
  3. Run (persists across reboots):
       tailscale funnel --bg 8000
  4. Your public MCP endpoint is:
       https://<machine>.<tailnet>.ts.net/mcp
EOF

if [ -n "$missing" ]; then
  echo
  err "Not started yet - fill these in $APP_DIR/.env first:$missing"
  echo "Then run:  sudo systemctl start $SERVICE_NAME"
else
  say "Starting service"
  sudo systemctl restart "$SERVICE_NAME"
  say "Running. Logs: journalctl -u $SERVICE_NAME -f"
fi
echo "------------------------------------------------------------"
