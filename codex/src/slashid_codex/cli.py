"""``slashid-codex hook`` (the client) and ``slashid-codex daemon``.

The hook path imports only the standard library, ``platformdirs`` and
``discovery``; the daemon's modules are imported inside ``main``. Plain
``json`` stands in for pydantic here for that reason.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol

from .discovery import (
    LOCK_FILE,
    STDERR_FILE,
    DaemonConnection,
    DaemonError,
    DaemonGone,
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
SPAWN_WAIT_S = 5.0
# The backstop answers this long after the deadline, if the client has not.
BACKSTOP_GRACE_S = 0.2
# A squatter on a dead daemon's port that never answers costs at most this.
PING_TIMEOUT_S = 1.0
SHUTDOWN_TIMEOUT_S = 2.0
POLL_S = 0.05
MAX_STDIN_BYTES = 10 * 1024 * 1024
_DRAIN_CHUNK = 65536

DAEMON_UNAVAILABLE = "Failed to start the SlashID Codex daemon."
DAEMON_TIMEOUT = "The SlashID Codex daemon gave no verdict in time."
DAEMON_BROKEN = "The connection to the SlashID Codex daemon broke."
DAEMON_BAD_ANSWER = "The SlashID Codex daemon gave an invalid answer."
PAYLOAD_TOO_LARGE = "The Codex hook payload is too large."
HOOK_FAILED = "The SlashID Codex hook failed."
HOOK_TIMEOUT = "The SlashID Codex hook ran out of time."
BAD_ARGUMENTS = "The SlashID Codex hook is misconfigured."


class _Process(Protocol):
    def poll(self) -> int | None: ...


Spawn = Callable[[list[str], Path, Path], _Process]


def _spawn(argv: list[str], cwd: Path, stderr_path: Path) -> _Process:
    return spawn_daemon(argv, cwd=cwd, stderr_path=stderr_path)


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
        self._key = spawn_key(self._config_path)

    def run(self, event: str, stdin: BinaryIO, *, deadline: float) -> str:
        preflight = event in PREFLIGHT_EVENTS

        def fail(cause: str) -> str:
            if not preflight or fail_mode(self._config_path) == "allow":
                return "{}"
            return _block(cause)

        payload = stdin.read(MAX_STDIN_BYTES + 1)
        if len(payload) > MAX_STDIN_BYTES:
            # Codex may block writing the rest.
            while stdin.read(_DRAIN_CHUNK):
                pass
            return fail(PAYLOAD_TOO_LARGE)
        ensure_state_dir(self._state)
        # A connection closed with no answer means the daemon is exiting; the
        # next discovery finds or starts another.
        for retry in (True, False):
            conn = self.connect(deadline, wait=preflight)
            if conn is None:
                return fail(DAEMON_UNAVAILABLE)
            try:
                status, body = conn.post(f"/hooks/{event}", payload, deadline=deadline)
                break
            except TimeoutError:
                return fail(DAEMON_TIMEOUT)
            except DaemonGone:
                if not retry:
                    return fail(DAEMON_BROKEN)
            except DaemonError:
                return fail(DAEMON_BROKEN)
            finally:
                conn.close()
        else:
            return fail(DAEMON_BROKEN)
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
            try:
                conn = connect(info, deadline=min(deadline, self._monotonic() + PING_TIMEOUT_S))
            except TimeoutError:
                if not lock_held(self._state / LOCK_FILE):
                    conn = None
                else:
                    # A live daemon, slow to answer: a new one could not take the lock.
                    conn = None
                    with contextlib.suppress(TimeoutError):
                        conn = connect(info, deadline=deadline)
                    if conn is None:
                        return None
            if conn is not None:
                if info.version == self._version:
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
        process = self._spawn(self._argv(), self._state, self._state / STDERR_FILE)
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
            ):
                tried.add((info.port, info.secret))
                with contextlib.suppress(TimeoutError):
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
        if name == "daemon":
            command.add_argument("--config-check-seconds", type=float)
        if name == "hook":
            command.add_argument(
                "--event", choices=[*PREFLIGHT_EVENTS, *TRIGGER_EVENTS], required=True
            )
    return parser


class _Once:
    """Writes the first answer only: the client's or the backstop's."""

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self._lock = threading.Lock()
        self._done = False

    def __call__(self, text: str) -> bool:
        with self._lock:
            if self._done:
                return False
            self._done = True
            self._write(text)
            return True


def _stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def run_hook(
    event: str,
    config_path: Path,
    state: Path | None,
    codex_home: Path | None,
    stdin: BinaryIO,
    *,
    deadlines: dict[str, float] = DEADLINES,
    spawn: Spawn = _spawn,
    write: Callable[[str], None] = _stdout,
    exit: Callable[[int], None] = os._exit,
) -> None:
    """Writes one valid JSON answer for Codex. Socket timeouts are per
    operation, so a backstop answers at the deadline and exits whatever the
    client is blocked on: Codex killing the hook would let the action through."""
    preflight = event in PREFLIGHT_EVENTS
    deadline = time.monotonic() + deadlines[event]
    once = _Once(write)
    # ``deny`` until the config is read.
    timeout_answer = [_block(HOOK_TIMEOUT) if preflight else "{}"]

    def backstop() -> None:
        if once(timeout_answer[0]):
            exit(0)

    timer = threading.Timer(deadlines[event] + BACKSTOP_GRACE_S, backstop)
    timer.daemon = True
    timer.start()
    try:
        if preflight and fail_mode(config_path) == "allow":
            timeout_answer[0] = "{}"
        try:
            client = Client(config_path, state_dir(state), codex_home, spawn=spawn)
            answer = client.run(event, stdin, deadline=deadline)
        except Exception:
            answer = _block(HOOK_FAILED) if timeout_answer[0] != "{}" else "{}"
        once(answer)
    finally:
        timer.cancel()


def _argument(args: list[str], name: str) -> str | None:
    """``--name value`` or ``--name=value`` from arguments that do not parse."""
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{name}="):
            return arg.partition("=")[2]
    return None


def _bad_arguments_answer(args: list[str]) -> str:
    """``block`` on ``Stop`` would continue the turn: only a preflight event blocks."""
    if _argument(args, "--event") not in PREFLIGHT_EVENTS:
        return "{}"
    config = _argument(args, "--config")
    if config is not None and fail_mode(Path(config)) == "allow":
        return "{}"
    return _block(BAD_ARGUMENTS)


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parser().parse_args(args_list)
    except _ArgumentError as exc:
        if args_list[:1] == ["hook"]:
            # A hook always exits 0 with valid JSON.
            _stdout(_bad_arguments_answer(args_list))
            return 0
        print(f"slashid-codex: {exc}", file=sys.stderr)
        return 2
    if args.command == "hook":
        run_hook(args.event, args.config, args.state_dir, args.codex_home, sys.stdin.buffer)
        return 0
    from .daemon import run_daemon

    return run_daemon(
        args.config.absolute(),
        state_dir=state_dir(args.state_dir).absolute(),
        codex_home=args.codex_home.absolute() if args.codex_home is not None else None,
        config_check_s=args.config_check_seconds,
    )
