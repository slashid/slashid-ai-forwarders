"""Configured MCP servers, from ``codex mcp list --json``, best effort. Only
``name`` and ``enabled`` are read: ``command``, ``args`` and ``env`` never
leave the machine."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from slashid_ai_forwarder_core.events import AIToolServer
from slashid_ai_forwarder_core.normalize._base import _LenientModel
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool

log = logging.getLogger(__name__)

TIMEOUT_S = 5.0
MAX_OUTPUT_BYTES = 1 << 20
CACHE_S = 600.0
# macOS and Windows bundle paths are unverified.
_LINUX_BUNDLE = Path("/usr/lib/chatgpt/resources/codex")
_MACOS_BUNDLE = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
_POSIX = sys.platform != "win32"


class _Server(_LenientModel):
    name: str
    enabled: bool = False


_SERVERS = TypeAdapter(list[_Server])


def parse_servers(raw: bytes) -> list[AIToolServer]:
    """Enabled servers, with the ids their tools' calls declare."""
    try:
        listed = _SERVERS.validate_json(raw)
    except ValidationError:
        return []
    servers: list[AIToolServer] = []
    for entry in listed:
        if not entry.enabled or not entry.name:
            continue
        _tool, server = resolve_tool(f"mcp__{entry.name}__tool")
        # A name with ``__`` cannot be told apart in tool names.
        if server.name == entry.name:
            servers.append(server)
    return servers


def find_codex(
    configured: Path | None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
    env: Mapping[str, str] = os.environ,
    is_file: Callable[[Path], bool] = Path.is_file,
) -> Path | None:
    """``codex_bin``, else ``codex`` on ``PATH``, else the desktop app's bundle."""
    if configured is not None:
        return configured
    if (found := which("codex")) is not None:
        return Path(found)
    bundle: Path | None = None
    if platform == "darwin":
        bundle = _MACOS_BUNDLE
    elif platform == "win32":
        if local := env.get("LOCALAPPDATA"):
            bundle = Path(local) / "Programs" / "ChatGPT" / "resources" / "codex.exe"
    else:
        bundle = _LINUX_BUNDLE
    return bundle if bundle is not None and is_file(bundle) else None


class McpServers:
    """The listing, refreshed at most every ``CACHE_S``; any failure is an empty list."""

    def __init__(
        self,
        codex_bin: Path | None,
        *,
        codex_home: Path,
        find: Callable[[], Path | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._find = find or (lambda: find_codex(codex_bin))
        self._env = {**os.environ, "CODEX_HOME": str(codex_home)}
        self._clock = clock
        self._cached: list[AIToolServer] = []
        self._fetched_at: float | None = None

    async def get(self) -> list[AIToolServer]:
        now = self._clock()
        if self._fetched_at is None or now - self._fetched_at >= CACHE_S:
            self._cached = await self._list()
            self._fetched_at = now
        return list(self._cached)

    async def _list(self) -> list[AIToolServer]:
        binary = self._find()
        if binary is None:
            return []
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "mcp",
                "list",
                "--json",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._env,
                start_new_session=_POSIX,
            )
        except OSError as exc:
            log.info("codex mcp list: %s", exc)
            return []
        try:
            stdout = await asyncio.wait_for(_read(proc), TIMEOUT_S)
        except TimeoutError:
            _kill(proc)
            await proc.wait()
            log.info("codex mcp list timed out")
            return []
        if stdout is None:
            _kill(proc)
            await proc.wait()
            log.info("codex mcp list output over %d bytes", MAX_OUTPUT_BYTES)
            return []
        if proc.returncode != 0:
            log.info("codex mcp list exited %s", proc.returncode)
            return []
        return parse_servers(stdout)


async def _read(proc: asyncio.subprocess.Process) -> bytes | None:
    """Stdout to EOF, then the exit; ``None`` past ``MAX_OUTPUT_BYTES``."""
    assert proc.stdout is not None
    out = bytearray()
    while chunk := await proc.stdout.read(1 << 16):
        out += chunk
        if len(out) > MAX_OUTPUT_BYTES:
            return None
    await proc.wait()
    return bytes(out)


def _kill(proc: asyncio.subprocess.Process) -> None:
    """The whole group on POSIX: a child left holding stdout would keep the pipe open."""
    try:
        if _POSIX:
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
