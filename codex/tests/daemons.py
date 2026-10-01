"""Hook and daemon subprocesses on a temporary state dir and Codex home, so
the real ``~/.codex`` is never swept; every daemon is killed at teardown."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from slashid_codex.discovery import DaemonInfo, read_daemon_json

TOKEN = "t" * 32


class Daemons:
    def __init__(self, root: Path, **config: object) -> None:
        self.root = root
        self.state = root / "state"
        self.codex_home = root / "codex-home"
        self.codex_home.mkdir(parents=True, exist_ok=True)
        self.config = root / "config.toml"
        (root / "token").write_text(TOKEN)
        self.write_config(**config)

    def write_config(self, **overrides: object) -> None:
        values: dict[str, object] = {
            "endpoint": "https://api.example.test",
            "push_token_file": str(self.root / "token"),
            "user_id": "user-abc",
            "dry_run": True,
            # No ``codex mcp list`` against the real Codex.
            "codex_bin": str(self.root / "no-codex"),
            **overrides,
        }
        lines = [f"{key} = {_toml(value)}" for key, value in values.items()]
        self.config.write_text("\n".join(lines) + "\n")

    def args(self, *command: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "slashid_codex",
            *command,
            "--config",
            str(self.config),
            "--state-dir",
            str(self.state),
            "--codex-home",
            str(self.codex_home),
        ]

    def hook(
        self, event: str, payload: bytes, *, timeout: float = 30
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            self.args("hook", "--event", event),
            input=payload,
            capture_output=True,
            timeout=timeout,
            check=True,
        )

    def info(self) -> DaemonInfo | None:
        return read_daemon_json(self.state)

    def wait_info(self, timeout: float = 10, *, other_than: int | None = None) -> DaemonInfo:
        give_up = time.monotonic() + timeout
        while time.monotonic() < give_up:
            info = self.info()
            if info is not None and info.pid != other_than:
                return info
            time.sleep(0.05)
        raise AssertionError("no daemon.json")

    def pids(self) -> list[int]:
        """Live daemon processes for this state dir (Linux), else the file's pid."""
        proc = Path("/proc")
        if not proc.is_dir():
            info = self.info()
            return [info.pid] if info is not None and alive(info.pid) else []
        marker = str(self.state).encode()
        found: list[int] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes()
                state = (entry / "stat").read_text().rsplit(")", 1)[1].split()[0]
            except OSError:
                continue
            if marker in cmdline and b"\0daemon\0" in cmdline and state != "Z":
                found.append(int(entry.name))
        return found

    def kill_all(self) -> None:
        for pid in self.pids():
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        info = self.info()
        # Fake daemons write the test's own pid.
        if info is not None and info.pid != os.getpid() and alive(info.pid):
            with contextlib.suppress(OSError):
                os.kill(info.pid, signal.SIGKILL)

    def log(self) -> str:
        try:
            return (self.state / "daemon.log").read_text()
        except FileNotFoundError:
            return ""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        with contextlib.suppress(OSError):
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    return True


def wait_dead(pid: int, timeout: float = 10) -> bool:
    give_up = time.monotonic() + timeout
    while time.monotonic() < give_up:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
