"""``slashid-codex hook`` (the client) and ``slashid-codex daemon``.

The hook path imports only the standard library, ``platformdirs`` and
``discovery``; the daemon's modules are imported inside ``main``. Plain
``json`` stands in for pydantic here for that reason.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol

from .discovery import (
    LOCK_FILE,
    LOG_FILE,
    DaemonConnection,
    DaemonError,
    config_digest,
    connect,
    ensure_state_dir,
    in_backoff,
    lock_held,
    package_version,
    read_daemon_json,
    record_spawn_failure,
    spawn_daemon,
    spawn_key,
    state_dir,
)

PREFLIGHT_EVENTS = ("UserPromptSubmit", "PreToolUse")
TRIGGER_EVENTS = ("Stop", "SessionStart", "SessionEnd")
# Inside Codex's hook timeouts (10 s, 5 s, 3 s).
DEADLINES: dict[str, float] = {
    "UserPromptSubmit": 9.0,
    "PreToolUse": 9.0,
    "Stop": 4.0,
    "SessionStart": 4.0,
    "SessionEnd": 2.5,
}
SPAWN_WAIT_S = 1.5
# A squatter on a dead daemon's port that never answers costs at most this.
PING_TIMEOUT_S = 1.0
SHUTDOWN_TIMEOUT_S = 2.0
POLL_S = 0.05
MAX_STDIN_BYTES = 10 * 1024 * 1024

DAEMON_UNAVAILABLE = "Failed to start the SlashID Codex daemon."
DAEMON_TIMEOUT = "The SlashID Codex daemon gave no verdict in time."
DAEMON_BROKEN = "The connection to the SlashID Codex daemon broke."
DAEMON_BAD_ANSWER = "The SlashID Codex daemon gave an invalid answer."
PAYLOAD_TOO_LARGE = "The Codex hook payload is too large."
HOOK_FAILED = "The SlashID Codex hook failed."
BAD_ARGUMENTS = "The SlashID Codex hook is misconfigured."


class _Process(Protocol):
    def poll(self) -> int | None: ...


Spawn = Callable[[list[str], Path, Path], _Process]


def _spawn(argv: list[str], cwd: Path, log_path: Path) -> _Process:
    return spawn_daemon(argv, cwd=cwd, log_path=log_path)


def fail_mode(config_path: Path) -> str:
    """``verdict_fail_mode`` from the config, ``deny`` unless it says ``allow``."""
    try:
        value = tomllib.loads(config_path.read_text(encoding="utf-8")).get("verdict_fail_mode")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return "deny"
    return "allow" if value == "allow" else "deny"


def _block(reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason})


def _verdict(body: bytes) -> str | None:
    """The daemon's verdict re-serialised, or ``None`` if it is not one."""
    try:
        value = json.loads(body)
    except ValueError:
        return None
    if value == {}:
        return "{}"
    if (
        isinstance(value, dict)
        and set(value) == {"decision", "reason"}
        and value["decision"] == "block"
        and isinstance(value["reason"], str)
    ):
        return _block(value["reason"])
    return None


class Client:
    def __init__(
        self,
        config_path: Path,
        state: Path,
        codex_home: Path | None,
        *,
        spawn: Spawn = _spawn,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config_path = config_path.absolute()
        self._state = state.absolute()
        self._codex_home = codex_home.absolute() if codex_home is not None else None
        self._spawn = spawn
        self._monotonic = monotonic
        self._sleep = sleep
        self._version = package_version()
        self._digest = config_digest(self._config_path)
        self._key = spawn_key(self._config_path)

    def run(self, event: str, stdin: BinaryIO, *, deadline: float) -> str:
        preflight = event in PREFLIGHT_EVENTS

        def fail(cause: str) -> str:
            if not preflight or fail_mode(self._config_path) == "allow":
                return "{}"
            return _block(cause)

        payload = stdin.read(MAX_STDIN_BYTES + 1)
        if len(payload) > MAX_STDIN_BYTES:
            return fail(PAYLOAD_TOO_LARGE)
        ensure_state_dir(self._state)
        conn = self.connect(deadline, wait=preflight)
        if conn is None:
            return fail(DAEMON_UNAVAILABLE)
        try:
            status, body = conn.post(f"/hooks/{event}", payload, deadline=deadline)
        except TimeoutError:
            return fail(DAEMON_TIMEOUT)
        except DaemonError:
            return fail(DAEMON_BROKEN)
        finally:
            conn.close()
        if not preflight:
            return "{}"
        verdict = _verdict(body) if status == 200 else None
        return verdict if verdict is not None else fail(DAEMON_BAD_ANSWER)

    def connect(self, deadline: float, *, wait: bool) -> DaemonConnection | None:
        """A verified connection to a current daemon. Starts one if needed;
        with ``wait`` false, returns ``None`` right after starting it."""
        tried: set[tuple[int, str]] = set()
        restarting = False
        if (info := read_daemon_json(self._state)) is not None:
            tried.add((info.port, info.secret))
            conn = connect(info, deadline=min(deadline, self._monotonic() + PING_TIMEOUT_S))
            if conn is not None:
                if info.version == self._version and info.config_digest == self._digest:
                    return conn
                # Returns once it has deleted daemon.json and released the lock.
                restarting = True
                with contextlib.suppress(DaemonError, TimeoutError):
                    conn.post(
                        "/shutdown",
                        b"",
                        deadline=min(deadline, self._monotonic() + SHUTDOWN_TIMEOUT_S),
                    )
                conn.close()
        if not restarting and in_backoff(self._state, self._key):
            return None
        process = self._spawn(self._argv(), self._state, self._state / LOG_FILE)
        if not wait:
            return None
        return self._await_spawn(process, deadline, tried)

    def _await_spawn(
        self, process: _Process, deadline: float, tried: set[tuple[int, str]]
    ) -> DaemonConnection | None:
        give_up = min(deadline, self._monotonic() + SPAWN_WAIT_S)
        while True:
            info = read_daemon_json(self._state)
            if (
                info is not None
                and (info.port, info.secret) not in tried
                and info.version == self._version
                and info.config_digest == self._digest
            ):
                tried.add((info.port, info.secret))
                if (conn := connect(info, deadline=deadline)) is not None:
                    return conn
            code = process.poll()
            if code not in (None, 0):
                record_spawn_failure(self._state, self._key)
                return None
            if self._monotonic() >= give_up:
                break
            self._sleep(POLL_S)
        # A live daemon holding the lock (ours, still starting, or another) is not a failure.
        if not lock_held(self._state / LOCK_FILE):
            record_spawn_failure(self._state, self._key)
        return None

    def _argv(self) -> list[str]:
        argv = [sys.executable, "-m", "slashid_codex", "daemon"]
        argv += ["--config", str(self._config_path), "--state-dir", str(self._state)]
        if self._codex_home is not None:
            argv += ["--codex-home", str(self._codex_home)]
        return argv


class _ArgumentError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="slashid-codex")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("hook", "daemon"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        # Tests and development only.
        command.add_argument("--state-dir", type=Path)
        command.add_argument("--codex-home", type=Path)
        if name == "hook":
            command.add_argument(
                "--event", choices=[*PREFLIGHT_EVENTS, *TRIGGER_EVENTS], required=True
            )
    return parser


def run_hook(
    event: str,
    config_path: Path,
    state: Path | None,
    codex_home: Path | None,
    stdin: BinaryIO,
    *,
    deadlines: dict[str, float] = DEADLINES,
    spawn: Spawn = _spawn,
) -> str:
    """Always valid JSON for Codex."""
    deadline = time.monotonic() + deadlines[event]
    try:
        client = Client(config_path, state_dir(state), codex_home, spawn=spawn)
        return client.run(event, stdin, deadline=deadline)
    except Exception:
        if event in PREFLIGHT_EVENTS and fail_mode(config_path) == "deny":
            return _block(HOOK_FAILED)
        return "{}"


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(args_list)
    except _ArgumentError as exc:
        if args_list[:1] == ["hook"]:
            # A hook always exits 0 with valid JSON.
            print(_block(BAD_ARGUMENTS), flush=True)
            return 0
        print(f"slashid-codex: {exc}", file=sys.stderr)
        return 2
    if args.command == "hook":
        stdin = sys.stdin.buffer
        print(run_hook(args.event, args.config, args.state_dir, args.codex_home, stdin), flush=True)
        return 0
    from .daemon import run_daemon

    return run_daemon(
        args.config.absolute(),
        state_dir=state_dir(args.state_dir).absolute(),
        codex_home=args.codex_home.absolute() if args.codex_home is not None else None,
    )
