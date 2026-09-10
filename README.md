# pc-shell-mcp

Minimal remote Linux shell over MCP for ChatGPT/other remote MCP clients.

## Why this build

- **Stable, free public URL:** Tailscale Funnel (`https://<machine>.<tailnet>.ts.net/mcp`), no domain or inbound port forwarding.
- **OAuth only:** GitHub OAuth + explicit GitHub username allow-list. No static production token and no token in the URL.
- **Current stack:** FastMCP `4.0.3`, pinned exactly.
- **Stateless tool calls:** each command gets an explicit `cwd`; concurrent clients cannot overwrite a shared shell session.
- **Bounded output + hard timeout:** stdout/stderr are continuously drained with fixed memory use, and timeout kills the command process group.
- **Encrypted OAuth storage:** client registrations/tokens persist on disk encrypted at rest.
- **One tool:** `run_command(command, cwd=None, timeout=60)`.

> This is still a remote shell. An allowed GitHub account can run arbitrary commands with the permissions of the Linux user running the service.

## Install

```bash
git clone https://github.com/Satianurag/pc-shell-mcp.git
cd pc-shell-mcp
./install.sh
```

Then edit `.env` and fill:

```dotenv
GH_CLIENT_ID=...
GH_CLIENT_SECRET=...
ALLOWED_GITHUB_USERS=Satianurag
MCP_BASE_URL=https://<machine>.<tailnet>.ts.net
```

Create the GitHub OAuth App with:

- Homepage: `MCP_BASE_URL`
- Callback: `MCP_BASE_URL/auth/callback`

Start the service:

```bash
sudo systemctl restart pc-shell
journalctl -u pc-shell -f
```

## Fixed public endpoint with Tailscale Funnel

Install/sign in to Tailscale, enable Funnel for the tailnet, then run once:

```bash
sudo tailscale funnel --bg 8000
```

The MCP URL is:

```text
https://<machine>.<tailnet>.ts.net/mcp
```

`--bg` persists the Funnel configuration across reboots/Tailscale restarts. Keep the server bound to `127.0.0.1`; Funnel proxies to it locally.

## Configuration

Required: `GH_CLIENT_ID`, `GH_CLIENT_SECRET`, `ALLOWED_GITHUB_USERS`, `MCP_BASE_URL`, `MCP_JWT_SIGNING_KEY`, `MCP_STORAGE_KEY`.

Optional limits: `MCP_PORT=8000`, `MCP_DEFAULT_TIMEOUT=60`, `MCP_MAX_TIMEOUT=600`, `MCP_MAX_OUTPUT=65536`, `MCP_MAX_CONCURRENT=4`, `MCP_OAUTH_DIR=~/.pc-shell/oauth`.

## Update

```bash
git pull
./install.sh
sudo systemctl restart pc-shell
```

MIT licensed.
