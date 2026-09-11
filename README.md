# pc-shell-mcp

Minimal remote shell over MCP for ChatGPT/other remote MCP clients.

## Design

- Stable public HTTPS URL via Tailscale Funnel: `https://<machine>.<tailnet>.ts.net/mcp`
- GitHub OAuth with an explicit GitHub username allow-list
- FastMCP `4.0.3`, pinned exactly
- One stateless tool: `run_command(command, cwd=None, timeout=60)`
- Bounded concurrency/output and process-group timeout cleanup
- Encrypted persisted OAuth state
- Native background service: macOS `launchd`, Linux `systemd`

> This is a remote shell. An allowed GitHub account can run arbitrary commands with the permissions of the local user running the service.

## Install

```bash
git clone https://github.com/Satianurag/pc-shell-mcp.git
cd pc-shell-mcp
./install.sh
```

On the first run, `.env` is created. Fill these values and rerun `./install.sh`:

```dotenv
GH_CLIENT_ID=...
GH_CLIENT_SECRET=...
ALLOWED_GITHUB_USERS=Satianurag
MCP_BASE_URL=https://<machine>.<tailnet>.ts.net
```

`MCP_JWT_SIGNING_KEY` and `MCP_STORAGE_KEY` are generated automatically.

GitHub OAuth App settings:

- Homepage URL: `MCP_BASE_URL`
- Authorization callback URL: `MCP_BASE_URL/auth/callback`

## macOS

The source checkout can live in `Documents`, but macOS protects that folder from unattended background processes. The installer therefore deploys a self-contained runtime to:

```text
~/Library/Application Support/pc-shell-mcp
```

The LaunchAgent `com.satianurag.pc-shell-mcp` runs only from that runtime. This avoids requiring Full Disk Access merely to start the MCP server. Rerun `./install.sh` after changing the source or `.env`; it synchronizes the runtime and restarts the agent.

```bash
launchctl print "gui/$(id -u)/com.satianurag.pc-shell-mcp"
tail -f ~/Library/Logs/pc-shell-mcp.err.log
```

The installer captures the interactive `PATH` so shell commands can find Homebrew and user-installed CLI tools. macOS privacy controls can still restrict remote commands that themselves access protected folders such as Documents or Desktop; grant only the access you actually need.

## Linux

```bash
sudo systemctl status pc-shell
sudo systemctl restart pc-shell
journalctl -u pc-shell -f
```

## Stable public endpoint

Install/sign in to Tailscale, enable Funnel for the tailnet, then run once:

```bash
tailscale funnel --bg 8000
```

The ChatGPT MCP Server URL is:

```text
https://<machine>.<tailnet>.ts.net/mcp
```

Keep `MCP_BASE_URL` as the origin only (`https://<machine>.<tailnet>.ts.net`), without `/mcp`.

## Verify before adding to ChatGPT

```bash
curl -i http://127.0.0.1:8000/mcp
curl -i "${MCP_BASE_URL}/mcp"
curl -i "${MCP_BASE_URL}/.well-known/oauth-protected-resource/mcp"
curl -i "${MCP_BASE_URL}/.well-known/oauth-authorization-server"
```

An unauthenticated `401` from `/mcp` is expected when OAuth is healthy; the public endpoint should not return `502`.

## Configuration

Required: `GH_CLIENT_ID`, `GH_CLIENT_SECRET`, `ALLOWED_GITHUB_USERS`, `MCP_BASE_URL`, `MCP_JWT_SIGNING_KEY`, `MCP_STORAGE_KEY`.

Optional limits: `MCP_HOST=127.0.0.1`, `MCP_PORT=8000`, `MCP_DEFAULT_TIMEOUT=60`, `MCP_MAX_TIMEOUT=600`, `MCP_MAX_OUTPUT=0` (0 = unlimited), `MCP_MAX_CONCURRENT=4`, `MCP_OAUTH_DIR=~/.pc-shell/oauth`.

## Update

```bash
git pull --ff-only
./install.sh
```

MIT licensed.
