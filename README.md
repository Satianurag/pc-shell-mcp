# pc-shell-mcp (Linux)

A self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a Linux
shell as tools over an authenticated, Streamable-HTTP endpoint, built with
[FastMCP](https://gofastmcp.com). Designed to be reached by a remote/cloud MCP
client (ChatGPT, Notion, Claude) through a tunnel.

> ⚠️ **This is a remote shell.** Anyone who can authenticate can run commands as
> your Linux user. Restrict access with the GitHub allow-list, keep the static
> token secret, and prefer a private deployment.

## Authentication (MultiAuth)

One endpoint accepts **two kinds of credentials at the same time**, so both
interactive apps and machine clients work without extra setup:

| Method | For | How the credential is sent |
| --- | --- | --- |
| **GitHub OAuth 2.1 + PKCE** | Interactive clients (ChatGPT, Notion, Claude) | Browser login, then `Authorization: Bearer <oauth-token>` |
| **Static bearer token** | Machine / fallback access (scripts, existing connections) | `Authorization: Bearer <MCP_TOKEN>` **or** `?token=<MCP_TOKEN>` in the URL |

- OAuth is provided by FastMCP's `GitHubProvider` — an OAuth **proxy** in front of
  one pre-registered GitHub OAuth app — so clients that rely on discovery /
  Dynamic Client Registration work out of the box.
- Only GitHub accounts listed in `ALLOWED_GITHUB_USERS` may call tools.
- The static token is **optional**: leave `MCP_TOKEN` unset to run OAuth-only.
- OAuth logins **survive restarts** with no re-login — a stable
  `MCP_JWT_SIGNING_KEY` keeps issued tokens valid, and registered clients are
  persisted on disk (`MCP_OAUTH_STORE_DIR`).

## What changed vs the original

| Problem in the old version | Fix in this build |
| --- | --- |
| Single shared token only | **MultiAuth**: GitHub OAuth *and* an optional static token on one endpoint |
| Every call was a fresh subprocess — `cd` / `export` forgotten | **Persistent session**: cwd + exported env survive across calls |
| Long output lost its top (tail-only) | **Head+tail** kept with an explicit `...[N chars truncated]...` marker |
| Timeout returned empty output | **Partial output** returned on timeout |
| No logging | **Structured audit log** of every invocation (JSON lines) |
| Full `shell=True` only | Optional **command allow-list** |
| Bound `0.0.0.0` | Binds **127.0.0.1** by default (tunnel needs only loopback) |
| Manual run, dies on reboot | **systemd service** (auto-start + auto-restart) via `install.sh` |
| URL changed every restart | **Tailscale Funnel** for a free, persistent public URL |

## Tools

Every tool requires a valid credential (an allow-listed OAuth user, or the static token).

| Tool | Args | Returns |
| --- | --- | --- |
| `run_command` | `command: str`, `timeout: int = 60` | `exit_code, stdout, stderr, cwd, duration_ms, timed_out, stdout_truncated, stderr_truncated` |
| `reset_session` | — | `status, cwd` (resets cwd + env to defaults) |
| `get_system_info` | — | `platform, release, machine, python, shell, user, cwd, allowlist_enabled` |

## Prerequisites

- Python 3.10+ and `openssl`.
- A **GitHub OAuth App** (GitHub > Settings > Developer settings > OAuth Apps > New):
  - **Homepage URL**: your public base URL, e.g. `https://<machine>.<tailnet>.ts.net`
  - **Authorization callback URL**: that base URL + `/auth/callback`
  - Copy the **Client ID** and generate a **Client secret**.

## Quick start

```bash
git clone <your-repo> pc-shell-mcp
cd pc-shell-mcp
./install.sh
```

`install.sh` creates the venv, installs deps, and scaffolds `.env` with a freshly
generated `MCP_TOKEN` and `MCP_JWT_SIGNING_KEY`. Fill in your GitHub OAuth
credentials, allow-list, and real `MCP_BASE_URL`, then start the service — the
installer prints the exact commands. It installs a systemd unit that starts on
boot and restarts on crash.

## Manual run (dev)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then edit .env (see Configuration)
set -a; . ./.env; set +a
.venv/bin/python server.py
```

Listens on `http://127.0.0.1:8000/mcp`.

## Expose it: free + persistent (Tailscale Funnel)

No domain, no cost, stable URL that survives reboots:

```bash
tailscale funnel --bg 8000
```

Your public endpoint becomes `https://<machine>.<tailnet>.ts.net/mcp`. Tailscale
persists the funnel config across reboots and provisions a valid HTTPS cert
automatically. (Prerequisite: enable MagicDNS + HTTPS certificates and the
`funnel` node attribute in the Tailscale admin console.) Use this host in both
`MCP_BASE_URL` and your GitHub OAuth App callback URL.

## Configuration

All via environment variables (see `.env.example`).

**Required**

| Var | Purpose |
| --- | --- |
| `GH_CLIENT_ID` | GitHub OAuth App client ID |
| `GH_CLIENT_SECRET` | GitHub OAuth App client secret |
| `MCP_JWT_SIGNING_KEY` | Stable key signing issued tokens (keeps OAuth logins valid across restarts) |
| `ALLOWED_GITHUB_USERS` | Comma-separated GitHub usernames allowed to call tools |
| `MCP_BASE_URL` | Public base URL; must match the OAuth callback host |

**Optional**

| Var | Default | Purpose |
| --- | --- | --- |
| `MCP_TOKEN` | *(unset)* | Static bearer token for machine/fallback access; unset = OAuth-only |
| `MCP_OAUTH_STORE_DIR` | `~/.pc-shell/oauth` | Where registered OAuth clients are persisted |
| `MCP_HOST` | `127.0.0.1` | Bind address |
| `MCP_PORT` | `8000` | Port |
| `MCP_PATH` | `/mcp` | Mount path |
| `MCP_DEFAULT_TIMEOUT` | `60` | Per-command timeout (s) |
| `MCP_MAX_OUTPUT` | `40000` | Max chars per stream before truncation |
| `MCP_ALLOWLIST` | *(empty)* | Comma-separated command allow-list; empty = full shell |
| `MCP_LOG_FILE` | `~/.pc-shell/audit.log` | Audit log path |

## Notes & limits

- Session persistence captures the **cwd** and **exported** environment. Plain
  (non-exported) shell variables are not carried across calls, which matches how
  child processes normally see the environment.
- Server secrets (`GH_CLIENT_SECRET`, `GH_CLIENT_ID`, `MCP_JWT_SIGNING_KEY`,
  `MCP_TOKEN`) are stripped from the environment handed to shell commands.
- systemd hardening (`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`)
  reduces blast radius without containers. Adjust `ReadWritePaths` to taste.

## License

MIT — see [LICENSE](LICENSE).
