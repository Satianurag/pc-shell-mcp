# pc-shell-mcp (Linux)

A self-hosted [MCP](https://modelcontextprotocol.io) server that exposes a Linux
shell as tools over an authenticated, Streamable-HTTP endpoint, built with
[FastMCP](https://gofastmcp.com). Designed to be reached by a remote/cloud MCP
client (e.g. Notion) through a tunnel.

> ⚠️ **This is a remote shell.** Anyone with the bearer token and the URL can run
> commands as your user. Keep the token secret and prefer a private deployment.

## What changed vs the original

| Problem in the old version | Fix in this build |
| --- | --- |
| Every call was a fresh subprocess — `cd` / `export` forgotten | **Persistent session**: cwd + exported env survive across calls |
| Output kept only the last 20k chars (top of logs lost) | **Head+tail** kept with an explicit `...[N chars truncated]...` marker |
| Timeout returned empty output | **Partial output** returned on timeout |
| No logging | **Structured audit log** of every invocation (JSON lines) |
| Full `shell=True` only | Optional **command allow-list** |
| Bound `0.0.0.0` | Binds **127.0.0.1** by default (tunnel needs only loopback) |
| Manual run, dies on reboot | **systemd service** (auto-start + auto-restart) via `install.sh` |
| URL changed every restart | **Tailscale Funnel** for a free, persistent public URL |

## Tools

| Tool | Args | Returns |
| --- | --- | --- |
| `run_command` | `command: str`, `timeout: int = 60` | `exit_code, stdout, stderr, cwd, duration_ms, timed_out, stdout_truncated, stderr_truncated` |
| `reset_session` | — | resets cwd + env to defaults |
| `get_system_info` | — | OS/platform + current cwd |

## Quick start (one command)

```bash
git clone <your-repo> pc-shell-mcp
cd pc-shell-mcp
./install.sh
```

`install.sh` creates the venv, installs deps, generates an `MCP_TOKEN`, and
installs a systemd service that starts on boot and restarts on crash. It prints
your token and the next steps for exposing it.

## Manual run (dev)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export MCP_TOKEN="$(openssl rand -hex 32)"
.venv/bin/python server.py
```

Listens on `http://127.0.0.1:8000/mcp`.

## Expose it: free + persistent (Tailscale Funnel)

No domain, no cost, stable URL that survives reboots:

```bash
tailscale funnel --bg 8000
```

Your public endpoint becomes `https://<machine>.<tailnet>.ts.net/mcp`. Tailscale
persists the funnel config across reboots, and it provisions a valid HTTPS cert
automatically. (Prerequisite: enable MagicDNS + HTTPS certificates and the
`funnel` node attribute in the Tailscale admin console.)

## Configuration

All via environment variables (see `.env.example`):

| Var | Default | Purpose |
| --- | --- | --- |
| `MCP_TOKEN` | *(required)* | Bearer token clients must send |
| `MCP_HOST` | `127.0.0.1` | Bind address |
| `MCP_PORT` | `8000` | Port |
| `MCP_PATH` | `/mcp` | Mount path |
| `MCP_DEFAULT_TIMEOUT` | `60` | Per-command timeout (s) |
| `MCP_MAX_OUTPUT` | `40000` | Max chars per stream before truncation |
| `MCP_ALLOWLIST` | *(empty)* | Comma-separated allow-list; empty = full shell |
| `MCP_LOG_FILE` | `~/.pc-shell/audit.log` | Audit log path |

## Notes & limits

- Session persistence captures the **cwd** and **exported** environment. Plain
  (non-exported) shell variables are not carried across calls, which matches how
  child processes normally see the environment.
- `StaticTokenVerifier` is a single shared bearer token. For multi-user or
  stricter setups, upgrade to `JWTVerifier` / an OAuth provider (FastMCP
  supports both) — see gofastmcp.com auth docs.
- systemd hardening (`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`)
  reduces blast radius without containers. Adjust `ReadWritePaths` to taste.

## License

MIT — see [LICENSE](LICENSE).
