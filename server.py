"""Minimal, authenticated local shell MCP server."""

import asyncio
import os
import signal
import time
from contextlib import suppress
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.server.auth import AuthContext
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.middleware import AuthMiddleware
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

# Load a colocated .env when present. Real environment variables keep precedence.
load_dotenv(Path(__file__).with_name(".env"))

HOST = os.environ.get("MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("MCP_PORT", "8000"))
BASE_URL = os.environ["MCP_BASE_URL"].rstrip("/")
DEFAULT_TIMEOUT = int(os.environ.get("MCP_DEFAULT_TIMEOUT", "60"))
MAX_TIMEOUT = int(os.environ.get("MCP_MAX_TIMEOUT", "600"))
MAX_OUTPUT = int(os.environ.get("MCP_MAX_OUTPUT", "65536"))
MAX_CONCURRENT = int(os.environ.get("MCP_MAX_CONCURRENT", "4"))
OAUTH_DIR = Path(os.environ.get("MCP_OAUTH_DIR", "~/.pc-shell/oauth")).expanduser()
ALLOWED_USERS = {
    user.strip().lower()
    for user in os.environ["ALLOWED_GITHUB_USERS"].split(",")
    if user.strip()
}

if not ALLOWED_USERS:
    raise SystemExit("ALLOWED_GITHUB_USERS must contain at least one GitHub username")
if DEFAULT_TIMEOUT < 1 or MAX_TIMEOUT < DEFAULT_TIMEOUT:
    raise SystemExit("Require 1 <= MCP_DEFAULT_TIMEOUT <= MCP_MAX_TIMEOUT")
if MAX_OUTPUT < 1024 or MAX_CONCURRENT < 1:
    raise SystemExit("MCP_MAX_OUTPUT must be >= 1024 and MCP_MAX_CONCURRENT >= 1")

OAUTH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
OAUTH_DIR.chmod(0o700)
_store = FileTreeStore(
    data_directory=OAUTH_DIR,
    key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(OAUTH_DIR),
    collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(OAUTH_DIR),
)
_store = FernetEncryptionWrapper(
    key_value=_store,
    fernet=Fernet(os.environ["MCP_STORAGE_KEY"].encode()),
)

auth = GitHubProvider(
    client_id=os.environ["GH_CLIENT_ID"],
    client_secret=os.environ["GH_CLIENT_SECRET"],
    base_url=BASE_URL,
    jwt_signing_key=os.environ["MCP_JWT_SIGNING_KEY"],
    client_storage=_store,
    required_scopes=["read:user"],
)


def _allowed(ctx: AuthContext) -> bool:
    login = ((ctx.token.claims or {}).get("login") or "").lower() if ctx.token else ""
    return login in ALLOWED_USERS


mcp = FastMCP(
    "pc-shell",
    auth=auth,
    middleware=[AuthMiddleware(auth=_allowed)],
)
_slots = asyncio.Semaphore(MAX_CONCURRENT)
_SECRET_ENV = {"GH_CLIENT_SECRET", "MCP_JWT_SIGNING_KEY", "MCP_STORAGE_KEY"}


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _SECRET_ENV:
        env.pop(key, None)
    return env


async def _capture(stream: asyncio.StreamReader, limit: int) -> tuple[str, bool]:
    """Drain a stream with bounded memory, retaining the head and tail."""
    head_limit = (limit * 2) // 3
    tail_limit = limit - head_limit
    head, tail = bytearray(), bytearray()
    total = 0

    while chunk := await stream.read(8192):
        total += len(chunk)
        if len(head) < head_limit:
            take = min(head_limit - len(head), len(chunk))
            head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk and tail_limit:
            tail.extend(chunk)
            if len(tail) > tail_limit:
                del tail[:-tail_limit]

    truncated = total > limit
    if truncated:
        dropped = total - len(head) - len(tail)
        data = head + f"\n...[{dropped} bytes truncated]...\n".encode() + tail
    else:
        data = head + tail
    return data.decode(errors="replace"), truncated


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=0.5)
        return
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    await proc.wait()


@mcp.tool
async def run_command(command: str, cwd: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a non-interactive bash command as the service user.

    Calls are intentionally stateless. Pass ``cwd`` explicitly when a command must
    run in a particular directory. ``timeout`` must be between 1 and
    MCP_MAX_TIMEOUT seconds. Output is memory-bounded and keeps its head and tail.
    """
    if not command.strip():
        raise ValueError("command must not be empty")
    if timeout < 1 or timeout > MAX_TIMEOUT:
        raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT} seconds")

    workdir = Path(cwd or os.environ.get("HOME", "/")).expanduser().resolve()
    if not workdir.is_dir():
        raise ValueError(f"cwd is not a directory: {workdir}")

    started = time.monotonic()
    timed_out = False
    async with _slots:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=str(workdir),
            env=_child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        stdout_task = asyncio.create_task(_capture(proc.stdout, MAX_OUTPUT))
        stderr_task = asyncio.create_task(_capture(proc.stderr, MAX_OUTPUT))
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            await _kill_process_group(proc)
        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task

    if timed_out:
        stderr = (stderr + f"\n[timed out after {timeout}s]").lstrip("\n")

    return {
        "exit_code": -1 if timed_out else proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "cwd": str(workdir),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "timed_out": timed_out,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


if __name__ == "__main__":
    mcp.run(transport="http", host=HOST, port=PORT)
