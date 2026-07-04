"""
pc-shell MCP server (Linux) - MultiAuth build (OAuth GitHub + static token).

Exposes a Linux shell to MCP clients (ChatGPT, Notion, Claude) over Streamable
HTTP, protected by real OAuth 2.1 + PKCE via FastMCP's GitHub OAuth provider.

Why OAuth instead of a static ?token=:
  * No secret token in the URL / server logs / browser history.
  * Standard browser login flow that ChatGPT and Notion understand natively
    (FastMCP presents a DCR/CIMD-compliant face and proxies to ONE pre-
    registered GitHub OAuth app).
  * Access restricted to an explicit allow-list of GitHub usernames.

Persistence (survives restarts, no re-login needed):
  * jwt_signing_key (stable, from env) keeps issued tokens valid across restarts.
  * client_storage (FileTreeStore on disk) keeps registered OAuth clients.

API grounded in installed FastMCP 3.4.2 (signature introspected on host).
"""

import json
import logging
import os
import platform
import shlex
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_access_token
from key_value.aio.stores.filetree import FileTreeStore

# --------------------------------------------------------------------------- #
# Configuration (all via environment; see .env.example)
# --------------------------------------------------------------------------- #
HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))
MOUNT_PATH = os.environ.get("MCP_PATH", "/mcp")
DEFAULT_TIMEOUT = int(os.environ.get("MCP_DEFAULT_TIMEOUT", "60"))
MAX_OUTPUT = int(os.environ.get("MCP_MAX_OUTPUT", "40000"))
ALLOWLIST = [c.strip() for c in os.environ.get("MCP_ALLOWLIST", "").split(",") if c.strip()]
LOG_FILE = os.path.expanduser(
    os.environ.get("MCP_LOG_FILE", str(Path.home() / ".pc-shell" / "audit.log"))
)

# OAuth (GitHub) configuration
GH_CLIENT_ID = os.environ.get("GH_CLIENT_ID")
GH_CLIENT_SECRET = os.environ.get("GH_CLIENT_SECRET")
BASE_URL = os.environ.get("MCP_BASE_URL", "http://localhost:8000")
JWT_SIGNING_KEY = os.environ.get("MCP_JWT_SIGNING_KEY")
OAUTH_STORE_DIR = os.path.expanduser(
    os.environ.get("MCP_OAUTH_STORE_DIR", str(Path.home() / ".pc-shell" / "oauth"))
)
ALLOWED_USERS = {
    u.strip().lower()
    for u in os.environ.get("ALLOWED_GITHUB_USERS", "").split(",")
    if u.strip()
}

# Static owner token: machine / fallback access (e.g. the Notion connection).
# Optional -- if MCP_TOKEN is unset, only OAuth is enabled.
STATIC_TOKEN = os.environ.get("MCP_TOKEN")

if not GH_CLIENT_ID or not GH_CLIENT_SECRET:
    raise SystemExit(
        "Set GH_CLIENT_ID and GH_CLIENT_SECRET (from your GitHub OAuth App)."
    )
if not JWT_SIGNING_KEY:
    raise SystemExit(
        "Set MCP_JWT_SIGNING_KEY (python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    )
if not ALLOWED_USERS:
    raise SystemExit(
        "Set ALLOWED_GITHUB_USERS to your GitHub username(s), comma-separated."
    )

# --------------------------------------------------------------------------- #
# Audit logging
# --------------------------------------------------------------------------- #
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
logger = logging.getLogger("pc-shell")
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_FILE)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)


def _audit(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    try:
        logger.info(json.dumps(event, ensure_ascii=False))
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Authorization guard: only allow-listed GitHub users may call tools
# --------------------------------------------------------------------------- #
def _require_authorized_user():
    try:
        token = get_access_token()
    except Exception:
        token = None
    if token is None:
        _audit({"event": "denied", "reason": "no_token"})
        raise PermissionError("Not authorized: no valid credentials.")

    # Path 1: static owner token (machine / fallback access, e.g. Notion).
    if getattr(token, "client_id", None) == "owner":
        return "owner"

    # Path 2: OAuth (GitHub) -- must be an allow-listed GitHub account.
    claims = getattr(token, "claims", None) or {}
    login = (claims.get("login") or claims.get("preferred_username") or "").lower()
    if login and login in ALLOWED_USERS:
        return login

    _audit({"event": "denied", "login": login, "claim_keys": list(claims.keys())})
    raise PermissionError(
        "Not authorized: your GitHub account is not on this server's allow-list."
    )


# --------------------------------------------------------------------------- #
# Persistent session state (cwd + exported environment)
# --------------------------------------------------------------------------- #
_lock = threading.Lock()


def _initial_env() -> dict:
    env = dict(os.environ)
    for secret in ("GH_CLIENT_SECRET", "GH_CLIENT_ID", "MCP_JWT_SIGNING_KEY", "MCP_TOKEN"):
        env.pop(secret, None)  # never expose server secrets to child commands
    return env


_session = {"cwd": os.environ.get("HOME", os.getcwd()), "env": _initial_env()}


def _truncate(text: str, limit: int):
    """Keep the head AND tail of long output with a visible marker."""
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    head = (limit * 2) // 3
    tail = limit - head
    dropped = len(text) - limit
    return (
        text[:head]
        + f"\n...[{dropped} chars truncated]...\n"
        + text[-tail:],
        True,
    )


# --------------------------------------------------------------------------- #
# Server (OAuth via GitHub; persistent token + client storage)
# --------------------------------------------------------------------------- #
oauth_provider = GitHubProvider(
    client_id=GH_CLIENT_ID,
    client_secret=GH_CLIENT_SECRET,
    base_url=BASE_URL,
    redirect_path="/auth/callback",
    jwt_signing_key=JWT_SIGNING_KEY,
    client_storage=FileTreeStore(data_directory=OAUTH_STORE_DIR),
    require_authorization_consent="remember",
)

# MultiAuth: OAuth (interactive clients: ChatGPT / Notion / Claude) AND an
# optional static bearer token (machine / fallback access) on ONE endpoint.
# The OAuth provider owns all OAuth routes + metadata; StaticTokenVerifier only
# adds a second token-verification path. Requests are verified against the OAuth
# provider first, then the static token.
_verifiers = []
if STATIC_TOKEN:
    # Match the OAuth provider's required scopes so the static token passes the
    # transport-level scope check (MultiAuth inherits required_scopes from the
    # server). Real authorization is still enforced by _require_authorized_user.
    _owner_scopes = list(oauth_provider.required_scopes or []) or ["use"]
    _verifiers.append(
        StaticTokenVerifier(
            tokens={STATIC_TOKEN: {"client_id": "owner", "scopes": _owner_scopes}}
        )
    )
auth = MultiAuth(server=oauth_provider, verifiers=_verifiers)
mcp = FastMCP("pc-shell", auth=auth)


@mcp.tool
def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a shell command on the Linux host and return the result.

    The session is persistent: `cd` and exported environment variables set by
    one call are remembered for subsequent calls. Use `reset_session` to clear.

    Returns: exit_code, stdout, stderr, cwd, duration_ms, timed_out,
    stdout_truncated, stderr_truncated.
    """
    _require_authorized_user()
    if ALLOWLIST:
        try:
            first = (shlex.split(command) or [""])[0]
        except ValueError:
            first = ""
        if first and os.path.basename(first) not in ALLOWLIST and first not in ALLOWLIST:
            result = {
                "exit_code": 126,
                "stdout": "",
                "stderr": f"Command '{first}' is not in the allow-list.",
                "cwd": _session["cwd"],
                "duration_ms": 0,
                "timed_out": False,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }
            _audit({"command": command, "blocked": True})
            return result

    with _lock:
        cwd = _session["cwd"]
        env = _session["env"]
        run_cwd = cwd if os.path.isdir(cwd) else None

        with tempfile.TemporaryDirectory() as td:
            cwd_f = os.path.join(td, "cwd")
            env_f = os.path.join(td, "env")
            wrapper = (
                f'cd {shlex.quote(cwd)} 2>/dev/null || cd "$HOME"\n'
                f"{command}\n"
                f"__rc=$?\n"
                f"printf '%s' \"$PWD\" > {shlex.quote(cwd_f)} 2>/dev/null || true\n"
                f"env -0 > {shlex.quote(env_f)} 2>/dev/null || true\n"
                f"exit $__rc\n"
            )

            start = time.monotonic()
            timed_out = False
            try:
                proc = subprocess.run(
                    ["bash", "-c", wrapper],
                    cwd=run_cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                rc = proc.returncode
                out = proc.stdout or ""
                err = proc.stderr or ""
            except subprocess.TimeoutExpired as e:
                timed_out = True
                rc = -1
                out = e.stdout or ""
                err = e.stderr or ""
                if isinstance(out, bytes):
                    out = out.decode(errors="replace")
                if isinstance(err, bytes):
                    err = err.decode(errors="replace")
                err = (err + f"\n[timed out after {timeout}s; partial output above]").strip()
            duration = int((time.monotonic() - start) * 1000)

            try:
                if os.path.exists(cwd_f):
                    new_cwd = Path(cwd_f).read_text().strip()
                    if new_cwd and os.path.isdir(new_cwd):
                        _session["cwd"] = new_cwd
                if os.path.exists(env_f):
                    raw = Path(env_f).read_bytes()
                    new_env = {}
                    for pair in raw.split(b"\x00"):
                        if not pair:
                            continue
                        k, _, v = pair.partition(b"=")
                        new_env[k.decode(errors="replace")] = v.decode(errors="replace")
                    if new_env:
                        for secret in ("GH_CLIENT_SECRET", "GH_CLIENT_ID", "MCP_JWT_SIGNING_KEY", "MCP_TOKEN"):
                            new_env.pop(secret, None)
                        _session["env"] = new_env
            except Exception:
                pass

    out_t, out_trunc = _truncate(out, MAX_OUTPUT)
    err_t, err_trunc = _truncate(err, MAX_OUTPUT)
    result = {
        "exit_code": rc,
        "stdout": out_t,
        "stderr": err_t,
        "cwd": _session["cwd"],
        "duration_ms": duration,
        "timed_out": timed_out,
        "stdout_truncated": out_trunc,
        "stderr_truncated": err_trunc,
    }
    _audit(
        {
            "command": command,
            "exit_code": rc,
            "cwd": _session["cwd"],
            "duration_ms": duration,
            "timed_out": timed_out,
        }
    )
    return result


@mcp.tool
def reset_session() -> dict:
    """Reset the persistent shell session (cwd and environment) to defaults."""
    _require_authorized_user()
    with _lock:
        _session["cwd"] = os.environ.get("HOME", os.getcwd())
        _session["env"] = _initial_env()
    _audit({"event": "reset_session"})
    return {"status": "reset", "cwd": _session["cwd"]}


@mcp.tool
def get_system_info() -> dict:
    """Return host OS/platform details and current session working directory."""
    _require_authorized_user()
    return {
        "platform": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "shell": os.environ.get("SHELL", "/bin/bash"),
        "user": os.environ.get("USER"),
        "cwd": _session["cwd"],
        "allowlist_enabled": bool(ALLOWLIST),
    }


# --------------------------------------------------------------------------- #
# ASGI shim: accept the static owner token via ?token= (query string) in
# addition to the standard Authorization: Bearer header. Preserves backward
# compatibility with the existing Notion connection so it survives the switch
# to MultiAuth without reconnecting. OAuth flows are unaffected (they always
# use the Authorization header).
# --------------------------------------------------------------------------- #
class QueryTokenToHeaderMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = scope.get("headers") or []
            has_auth = any(k == b"authorization" for k, _ in headers)
            if not has_auth:
                from urllib.parse import parse_qs
                qs = scope.get("query_string", b"").decode("latin-1")
                tok = parse_qs(qs).get("token", [None])[0]
                if tok:
                    scope = dict(scope)
                    scope["headers"] = list(headers) + [
                        (b"authorization", ("Bearer " + tok).encode("latin-1"))
                    ]
        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    app = mcp.http_app(path=MOUNT_PATH)
    app = QueryTokenToHeaderMiddleware(app)
    uvicorn.run(app, host=HOST, port=PORT)
