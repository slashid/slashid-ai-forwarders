"""``UserPromptSubmit`` and ``PreToolUse`` → a SlashID preflight verdict
(spec "Enforcement")."""

from __future__ import annotations

import asyncio
import functools
import logging
import mimetypes
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AITool,
    AIToolServer,
    AIToolUse,
    OpenAIIdentityDetails,
    used_tools_of,
)
from slashid_ai_forwarder_core.files import hash_local_file
from slashid_ai_forwarder_core.normalize._base import _LenientModel
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool
from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedMessage,
)
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant
from slashid_ai_forwarder_core.reads import get_file_read_by_tool
from slashid_ai_forwarder_core.sink import PreflightError

from .attachments import parse_attachments
from .cache import SessionCache, locate
from .config import CodexConfig
from .cursor import RolloutCursor
from .envelope import declared_from_calls, failed_calls, mark_errors, used_declarations, wire_time
from .hooks import PreToolUseHook, UserPromptSubmitHook
from .rollout import CodexItem
from .state import FileRecordStore, RecordStoreBusy

log = logging.getLogger(__name__)

PARSED_AS = "codex-hook"
MAX_FILES = 50
MAX_TOTAL_BYTES = 200 * 1024 * 1024
# Of the verdict budget, the share hashing may spend.
HASH_BUDGET_SHARE = 0.5
PREPARE_WORKERS = 4

Provenance = Literal["tool_result", "attachment"]
_PreflightHook = UserPromptSubmitHook | PreToolUseHook


class Verdict(BaseModel):
    """Codex's hook output; empty allows."""

    model_config = ConfigDict(frozen=True)

    decision: Literal["block"] | None = None
    reason: str | None = None


def fail_verdict(config: CodexConfig, cause: str) -> Verdict:
    """No verdict: ``verdict_fail_mode`` decides. ``cause`` never carries payload content."""
    if config.verdict_fail_mode == "allow":
        return Verdict()
    return Verdict(decision="block", reason=cause)


class PreflightSink(Protocol):
    async def preflight(
        self, invocation: AIInvocationObservedV1, *, deadline: float
    ) -> list[str]: ...


def _unconsumed(
    head: RolloutCursor,
) -> tuple[tuple[NormalizedMessage, ...], list[str], list[CodexItem]]:
    """The history, and the tool results after its last answer with their items."""
    view = head.view()
    round_ids = [
        b.tool_use_id
        for m in after_last_assistant(view)
        for b in m.content
        if b.kind == "tool_result" and b.tool_use_id
    ]
    return view, round_ids, [i for call_id in round_ids for i in head.items_for(call_id)]


class _Workdir(_LenientModel):
    workdir: str | None = None


class _Hasher:
    """At most ``MAX_FILES`` files and ``MAX_TOTAL_BYTES`` per request; past
    that, entries go without hashes."""

    def __init__(
        self,
        max_file_bytes: int,
        *,
        hash_until: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_file_bytes = max_file_bytes
        self._hash_until = hash_until
        self._monotonic = monotonic
        self._files = 0
        self._bytes = 0

    def __call__(self, path: Path, provenance: Provenance) -> AIAccessedFile:
        remaining = MAX_TOTAL_BYTES - self._bytes
        late = self._hash_until is not None and self._monotonic() >= self._hash_until
        if self._files >= MAX_FILES or remaining <= 0 or late:
            return AIAccessedFile(
                name=str(path), media_type=mimetypes.guess_type(path.name)[0], provenance=provenance
            )
        entry = hash_local_file(
            path, max_bytes=min(self._max_file_bytes, remaining), provenance=provenance
        )
        self._files += 1
        if entry.content_hashes is not None:
            self._bytes += entry.byte_length or 0
        return entry


class _Pool:
    """Up to ``workers`` daemon threads, started as needed. A thread hung on a
    stuck mount costs this pool only, and does not hold up exit (the default
    executor's threads are joined at exit)."""

    def __init__(self, workers: int, name: str) -> None:
        self._workers = workers
        self._name = name
        self._jobs: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._threads = 0
        self._idle = 0

    def submit[T](self, fn: Callable[[], T]) -> Future[T]:
        future: Future[T] = Future()

        def job() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_exception(exc)

        with self._lock:
            if self._idle == 0 and self._threads < self._workers:
                self._threads += 1
                threading.Thread(
                    target=self._work, name=f"{self._name}-{self._threads}", daemon=True
                ).start()
            self._jobs.put(job)
        return future

    def _work(self) -> None:
        while True:
            with self._lock:
                self._idle += 1
            job = self._jobs.get()
            with self._lock:
                self._idle -= 1
            job()


class Preflight:
    def __init__(
        self,
        config: CodexConfig,
        sink: PreflightSink,
        records: FileRecordStore,
        sessions: SessionCache,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._sink = sink
        self._records = records
        self._sessions = sessions
        self._clock = clock
        self._monotonic = monotonic
        self._pool = _Pool(PREPARE_WORKERS, "codex-preflight")

    async def user_prompt_submit(
        self, hook: UserPromptSubmitHook, *, deadline: float | None = None
    ) -> Verdict:
        """``deadline`` is a ``time.monotonic()`` instant, taken when the hook
        arrived; default ``preflight_timeout_seconds`` from now."""
        return await self._run(self._prompt_invocation, hook, deadline)

    async def pre_tool_use(self, hook: PreToolUseHook, *, deadline: float | None = None) -> Verdict:
        return await self._run(self._tool_invocation, hook, deadline)

    async def _run[H: _PreflightHook](
        self,
        build: Callable[[H, float], AIInvocationObservedV1],
        hook: H,
        deadline: float | None,
    ) -> Verdict:
        """Files past half the budget go without hashes; preparation still
        running at the deadline gives no verdict."""
        now = self._monotonic()
        if deadline is None:
            deadline = now + self._config.preflight_timeout_seconds
        hash_until = now + (deadline - now) * HASH_BUDGET_SHARE
        try:
            invocation = await asyncio.wait_for(
                asyncio.wrap_future(self._pool.submit(functools.partial(build, hook, hash_until))),
                timeout=max(deadline - now, 0.0),
            )
        except TimeoutError:
            log.warning("preflight for %s: deadline exceeded preparing", hook.session_id)
            return fail_verdict(self._config, "SlashID preflight failed: deadline exceeded")
        return await self._ask(invocation, deadline)

    async def _ask(self, invocation: AIInvocationObservedV1, deadline: float) -> Verdict:
        try:
            reasons = await self._sink.preflight(invocation, deadline=deadline)
        except PreflightError as exc:
            log.warning("preflight %s failed: %s", invocation.request_id, exc)
            return fail_verdict(self._config, f"SlashID preflight failed: {exc}")
        if not reasons:
            return Verdict()
        return Verdict(decision="block", reason=" ".join(reasons))

    # ----------------------------------------------------------------------
    # Blocking parts, on a worker thread
    # ----------------------------------------------------------------------

    def _prompt_invocation(
        self, hook: UserPromptSubmitHook, hash_until: float
    ) -> AIInvocationObservedV1:
        """The prompt's attachments, plus what the model has not consumed
        yet: tool results left by an interrupted response."""
        hasher = self._hasher(hash_until)
        attachments = [hasher(Path(a.path), "attachment") for a in parse_attachments(hook.prompt)]
        if attachments:
            self._store(lambda: self._records.put_turn(hook.session_id, hook.turn_id, attachments))

        messages: list[NormalizedMessage] = []
        record_keys: list[str] = []
        if (read := self._read_head(hook, _unconsumed)) is not None:
            view, round_ids, items = read
            messages = mark_errors(view, failed_calls(items))
            record_keys = list(dict.fromkeys([*round_ids, *(i.id for i in items if i.id)]))
        tools, servers = declared_from_calls(messages)
        used = used_tools_of(
            NormalizedInvocation(
                input=NormalizedInvocationInput(
                    messages=messages, tools_declared=tools, tool_servers=servers
                )
            )
        )
        tools, servers = used_declarations(used, tools, servers)
        files = attachments + self._read_records(hook.session_id, record_keys)
        return self._invocation(
            hook,
            request_id=hook.turn_id,
            accessed_files=files,
            used_tools=used,
            tools=tools,
            servers=servers,
        )

    def _tool_invocation(self, hook: PreToolUseHook, hash_until: float) -> AIInvocationObservedV1:
        """Only the file this call reads."""
        workdir = self._workdir(hook)
        files: list[AIAccessedFile] = []
        path = get_file_read_by_tool(hook.tool_name, hook.tool_input, workdir, expand_home=True)
        if path is not None:
            entry = self._hasher(hash_until)(path, "tool_result")
            files.append(entry)
            self._store(
                lambda: self._records.put_call(
                    hook.session_id, hook.turn_id, hook.tool_use_id, entry
                )
            )
        try:
            tool, server = resolve_tool(hook.tool_name)
        except ValueError:
            return self._invocation(
                hook, request_id=f"{hook.turn_id}:{hook.tool_use_id}", accessed_files=files
            )
        return self._invocation(
            hook,
            request_id=f"{hook.turn_id}:{hook.tool_use_id}",
            accessed_files=files,
            requested=[AIToolUse(tool_id=tool.id, tool_use_id=hook.tool_use_id)],
            tools=[tool],
            servers=[server],
        )

    def _hasher(self, hash_until: float) -> _Hasher:
        return _Hasher(
            self._config.max_file_bytes, hash_until=hash_until, monotonic=self._monotonic
        )

    def _workdir(self, hook: PreToolUseHook) -> str:
        """The call's own ``workdir`` (function mode), else the payload's ``cwd``."""
        call = self._read_head(hook, lambda head: head.pending_call(hook.tool_use_id))
        if call is not None:
            try:
                workdir = _Workdir.model_validate_json(call.arguments).workdir
            except ValidationError:
                workdir = None
            if workdir:
                return workdir
        return hook.cwd

    def _read_head[T](self, hook: _PreflightHook, read: Callable[[RolloutCursor], T]) -> T | None:
        """``read`` of the session's head moved to the end of the rollout;
        ``None`` if there is no readable rollout."""
        path = locate(hook.session_id, hook.transcript_path, self._config.codex_home)
        if path is None:
            return None
        try:
            with self._sessions.session(hook.session_id, path) as session, session.lock:
                session.refresh()
                return read(session.head)
        except OSError as exc:
            log.warning("rollout %s unreadable: %s", path, exc)
            return None

    def _store(self, write: Callable[[], None]) -> None:
        """A record that cannot be written costs the event its file, not the verdict."""
        try:
            write()
        except (RecordStoreBusy, sqlite3.Error) as exc:
            log.warning("file record not written: %s", exc)

    def _read_records(self, session_id: str, tool_ids: list[str]) -> list[AIAccessedFile]:
        try:
            return self._records.for_round(session_id, [], tool_ids)
        except sqlite3.Error as exc:
            log.warning("file records not read: %s", exc)
            return []

    def _invocation(
        self,
        hook: _PreflightHook,
        *,
        request_id: str,
        accessed_files: list[AIAccessedFile],
        tools: list[AITool] | None = None,
        servers: list[AIToolServer] | None = None,
        used_tools: list[AIToolUse] | None = None,
        requested: list[AIToolUse] | None = None,
    ) -> AIInvocationObservedV1:
        return AIInvocationObservedV1(
            request_id=request_id,
            timestamp=wire_time(self._clock()),
            identity_details=OpenAIIdentityDetails(user_id=self._config.user_id),
            model=AIModel(id=hook.model or "unknown", provider="openai"),
            parsed_as=PARSED_AS,
            conversation_id=hook.session_id,
            accessed_files=accessed_files or None,
            used_tools=used_tools or None,
            requested_tool_uses=requested or None,
            available_tools=tools or None,
            available_tool_servers=servers or None,
        )
