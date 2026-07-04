"""
pc-shell MCP server (Linux) - hardened build.

Exposes a Linux shell to an MCP client (e.g. Notion) over Streamable HTTP.
Improvements over the original 20-line server:
  * Persistent session: working directory and exported env vars survive across
    calls (fixes the "cd/export forgotten every call" flaw).
  * Partial output on timeout (no more losing everything a command printed).
  * Head+tail output truncation with an explicit marker (no silently dropping
    the top of a build log).
  * Structured audit logging of every invocation.
  * Optional command allow-list.
  * Binds 127.0.0.1 by default (the tunnel only needs loopback).

API grounded in current FastMCP docs (gofastmcp.com): FastMCP, @mcp.tool,
mcp.run(transport="http", host, port, path); StaticTokenVerifier import path
verified against fastmcp 3.4.x.
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
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

# --------------------------------------------------------------------------- #
# Configuration (all via environment; see .env.example)
# --------------------------------------------------------------------------- #
TOKEN = os.environ.get("MCP_TOKEN")
if not TOKEN:
    raise SystemExit("Set MCP_TOKEN (generate one with: openssl rand -hex 32)")

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))
MOUNT_PATH = os.environ.get("MCP_PATH", "/mcp")
DEFAULT_TIMEOUT = int(os.environ.get("MCP_DEFAULT_TIMEOUT", "60"))
MAX_OUTPUT = int(os.environ.get("MCP_MAX_OUTPUT", "40000"))
ALLOWLIST = [c.strip() for c in os.environ.get("MCP_ALLOWLIST", "").split(",") if c.strip()]
LOG_FILE = os.path.expanduser(
    os.environ.get("MCP_LOG_FILE", str(Path.home() / ".pc-shell" / "audit.log"))
)

# --------------------------------------------------------------------------- #
# Audit logging (server-side; docs recommend stdlib logging for file output)
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
# Persistent session state (cwd + exported environment)
# --------------------------------------------------------------------------- #
_lock = threading.Lock()


def _initial_env() -> dict:
    env = dict(os.environ)
    env.pop("MCP_TOKEN", None)  # never expose the server token to child commands
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
# Server
# --------------------------------------------------------------------------- #
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, AuthenticatedUser
from starlette.requests import HTTPConnection
from starlette.authentication import AuthCredentials

class QueryParamBearerAuthBackend(BearerAuthBackend):
    async def authenticate(self, conn: HTTPConnection):
        auth_header = next(
            (conn.headers.get(key) for key in conn.headers if key.lower() == "authorization"),
            None,
        )
        token = None
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header[7:]
        else:
            token = conn.query_params.get("token")

        if not token:
            return None

        auth_info = await self.token_verifier.verify_token(token)
        if not auth_info:
            return None

        if auth_info.expires_at and auth_info.expires_at < int(time.time()):
            return None

        return AuthCredentials(auth_info.scopes), AuthenticatedUser(auth_info)

class CustomStaticTokenVerifier(StaticTokenVerifier):
    def get_middleware(self) -> list:
        return [
            Middleware(
                AuthenticationMiddleware,
                backend=QueryParamBearerAuthBackend(self),
            ),
            Middleware(AuthContextMiddleware),
        ]

auth = CustomStaticTokenVerifier(
    tokens={TOKEN: {"client_id": "owner", "scopes": ["use"]}}
)
mcp = FastMCP("pc-shell", auth=auth)


@mcp.tool
def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a shell command on the Linux host and return the result.

    The session is persistent: `cd` and exported environment variables set by
    one call are remembered for subsequent calls. Use `reset_session` to clear.

    Returns: exit_code, stdout, stderr, cwd, duration_ms, timed_out,
    stdout_truncated, stderr_truncated.
    """
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

            # Persist new cwd / exported env for the next call.
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
                        new_env.pop("MCP_TOKEN", None)
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
    with _lock:
        _session["cwd"] = os.environ.get("HOME", os.getcwd())
        _session["env"] = _initial_env()
    _audit({"event": "reset_session"})
    return {"status": "reset", "cwd": _session["cwd"]}


@mcp.tool
def get_system_info() -> dict:
    """Return host OS/platform details and current session working directory."""
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


if __name__ == "__main__":
    mcp.run(transport="http", host=HOST, port=PORT, path=MOUNT_PATH)
