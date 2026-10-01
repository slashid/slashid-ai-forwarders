"""Collection: triggers, batches pulled from the send cursor, the watermark,
the startup sweep (spec "Collection"). Runs on one worker thread with its
own event loop."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint, CheckpointStore

from .cache import Session, SessionCache, locate
from .config import CodexConfig
from .cursor import RolloutInvocation
from .events import SessionContext, build_event, record_keys
from .hooks import SessionEndHook, SessionStartHook, StopHook
from .mcp_servers import McpServers
from .rollout import RolloutLineError, SessionMeta, parse_line
from .state import FileRecordStore, RecordStoreBusy

log = logging.getLogger(__name__)

COLLECTION = "codex-rollouts"
BATCH_SIZE = 20
RETRY_DELAYS_S = (1.0, 5.0, 15.0)
RETENTION = timedelta(days=7)
_ROOTS = ("sessions", "archived_sessions")
# Queue priorities: a hook's session goes before the sweep's.
LIVE, SWEEP = 0, 1


class PushSink(Protocol):
    async def push(self, events: list[AIInvocationObservedV1]) -> int: ...


class CollectorPlatform(Protocol):
    def checkpoint_store(self, *, collection: str, document: str) -> CheckpointStore: ...
    def created_at(self) -> datetime: ...


@runtime_checkable
class CheckpointPruner(Protocol):
    def prune_checkpoints(self, *, collection: str, older_than: datetime) -> None: ...


@dataclass(frozen=True)
class Trigger:
    session_id: str
    transcript_path: Path | None = None
    ended: bool = False

    @classmethod
    def from_hook(cls, hook: SessionStartHook | StopHook | SessionEndHook) -> Trigger:
        return cls(hook.session_id, hook.transcript_path, isinstance(hook, SessionEndHook))


class Collector:
    def __init__(
        self,
        config: CodexConfig,
        sink: PushSink,
        records: FileRecordStore,
        platform: CollectorPlatform,
        cache: SessionCache,
        *,
        mcp: McpServers | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_published: Callable[[], None] = lambda: None,
    ) -> None:
        self._config = config
        self._sink = sink
        self._records = records
        self._platform = platform
        self._cache = cache
        self._mcp = mcp
        self._clock = clock
        self._sleep = sleep
        self._on_published = on_published
        self._queue: asyncio.PriorityQueue[tuple[int, int, Trigger]] = asyncio.PriorityQueue()
        self._seq = itertools.count()

    def watermark(self, session_id: str) -> Checkpoint:
        return self._store(session_id).load()

    def _store(self, session_id: str) -> CheckpointStore:
        return self._platform.checkpoint_store(collection=COLLECTION, document=session_id)

    # ----------------------------------------------------------------------
    # Queue
    # ----------------------------------------------------------------------

    def trigger(self, trigger: Trigger, *, priority: int = LIVE) -> None:
        """Call on the collector's loop."""
        self._queue.put_nowait((priority, next(self._seq), trigger))

    async def run(self) -> None:
        while True:
            await self._next()

    async def drain(self) -> None:
        """Until the queue is empty."""
        while not self._queue.empty():
            await self._next()

    async def _next(self) -> None:
        _, _, trigger = await self._queue.get()
        try:
            await self.collect(trigger)
        except Exception:
            log.exception("collection of %s failed", trigger.session_id)

    # ----------------------------------------------------------------------
    # One session
    # ----------------------------------------------------------------------

    async def collect(self, trigger: Trigger) -> None:
        path = locate(trigger.session_id, trigger.transcript_path, self._config.codex_home)
        if path is not None:
            session = self._cache.get(trigger.session_id, path)
            with session.lock:
                session.refresh()
            await self.process(session)
        if trigger.ended:
            self._cache.end(trigger.session_id)
        self._cache.evict()

    async def process(self, session: Session) -> bool:
        """Send batches until the send cursor reaches the end. A batch that
        still fails after its retries rewinds the cursor to the watermark."""
        while True:
            with session.lock:
                batch: list[RolloutInvocation] = []
                while len(batch) < BATCH_SIZE and (item := session.send.next_closed()) is not None:
                    batch.append(item)
                if not batch:
                    return True
                send = session.send
                context = SessionContext(
                    session.session_id,
                    originator=send.originator,
                    cli_version=send.cli_version,
                    history_truncated=send.history_truncated,
                )
                session.batch_in_flight = True
            try:
                sent = await self._send(context, batch)
            finally:
                session.batch_in_flight = False
            if not sent:
                with session.lock:
                    session.rewind_send(self.watermark(session.session_id))
                return False

    async def _send(self, context: SessionContext, batch: list[RolloutInvocation]) -> bool:
        if self._mcp is not None:
            context = replace(context, mcp_servers=tuple(await self._mcp.get()))
        events: list[AIInvocationObservedV1] | None = None
        for delay in (*RETRY_DELAYS_S, None):
            try:
                if events is None:
                    events = await self._build(context, batch)
                if events:
                    await self._sink.push(events)
                break
            except Exception as exc:
                log.warning("batch for %s failed: %r", context.session_id, exc)
                if delay is None:
                    return False
                await self._sleep(delay)
        self._commit(context.session_id, batch)
        if events:
            self._on_published()
        return True

    async def _build(
        self, context: SessionContext, batch: list[RolloutInvocation]
    ) -> list[AIInvocationObservedV1]:
        """A response that cannot be built is dropped; a database error fails the batch."""
        events: list[AIInvocationObservedV1] = []
        for invocation in batch:
            try:
                events.append(
                    await build_event(
                        invocation, context, config=self._config, records=self._records
                    )
                )
            except (sqlite3.Error, RecordStoreBusy):
                raise
            except Exception:
                log.exception("dropped response %s", invocation.response_id)
        return events

    def _commit(self, session_id: str, batch: list[RolloutInvocation]) -> None:
        """Watermark at the batch's last record, then the records it consumed
        and those of turns it finished."""
        last = batch[-1]
        try:
            self._store(session_id).save(Checkpoint(last.timestamp, last.response_id))
        except sqlite3.Error as exc:
            log.warning("watermark for %s not saved: %s", session_id, exc)
        turn_ids: dict[str, None] = {}
        tool_ids: dict[str, None] = {}
        finished: dict[str, None] = {}
        for invocation in batch:
            turns, tools = record_keys(invocation)
            turn_ids.update(dict.fromkeys(turns))
            tool_ids.update(dict.fromkeys(tools))
            finished.update(dict.fromkeys(invocation.finished_turn_ids))
        try:
            self._records.delete_keys(session_id, list(turn_ids), list(tool_ids))
            for turn_id in finished:
                self._records.delete_turn(session_id, turn_id)
        except (RecordStoreBusy, sqlite3.Error) as exc:
            log.warning("file records for %s not deleted: %s", session_id, exc)

    # ----------------------------------------------------------------------
    # Startup sweep
    # ----------------------------------------------------------------------

    def startup_sweep(self) -> int:
        """Queue every rollout modified in the last ``RETENTION`` and after the
        database was created, newest first, unless unmodified since its
        watermark; prune what is older. Returns how many were queued."""
        now = self._clock()
        cutoff = now - RETENTION
        self._prune(cutoff)
        bound = max(cutoff, self._platform.created_at())
        newest: dict[str, tuple[datetime, Path]] = {}
        for root in _ROOTS:
            for path in (self._config.codex_home / root).rglob("rollout-*.jsonl"):
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                except OSError:
                    continue
                if mtime <= bound or (session_id := _session_id(path)) is None:
                    continue
                if session_id not in newest or newest[session_id][0] < mtime:
                    newest[session_id] = (mtime, path)
        queued = 0
        for session_id, (mtime, path) in sorted(
            newest.items(), key=lambda item: item[1][0], reverse=True
        ):
            stamp = self.watermark(session_id).timestamp
            if stamp is not None and mtime <= stamp:
                continue
            self.trigger(Trigger(session_id, path), priority=SWEEP)
            queued += 1
        return queued

    def _prune(self, cutoff: datetime) -> None:
        try:
            self._records.delete_older_than(cutoff)
            if isinstance(self._platform, CheckpointPruner):
                self._platform.prune_checkpoints(collection=COLLECTION, older_than=cutoff)
        except (RecordStoreBusy, sqlite3.Error) as exc:
            log.warning("prune failed: %s", exc)


def _session_id(path: Path) -> str | None:
    """From the rollout's first line, its ``session_meta``."""
    try:
        with path.open("rb") as f:
            first = f.readline()
        line = parse_line(first)
    except (OSError, RolloutLineError):
        return None
    if line is None or not isinstance(line.payload, SessionMeta):
        return None
    return line.payload.session_id or line.payload.id


class Worker:
    """Collection's thread and event loop. ``open_collector`` runs on that
    loop, so the HTTP client it creates is the worker's own."""

    def __init__(
        self,
        open_collector: Callable[[], AbstractAsyncContextManager[Collector]],
        *,
        sweep: bool = True,
    ) -> None:
        self._open = open_collector
        self._sweep = sweep
        self._loop: asyncio.AbstractEventLoop | None = None
        self._collector: Collector | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._main, name="codex-collector", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()

    def submit(self, trigger: Trigger) -> None:
        """From any thread."""
        loop, collector = self._loop, self._collector
        if loop is not None and collector is not None:
            loop.call_soon_threadsafe(collector.trigger, trigger)

    def stop(self, timeout: float = 5.0) -> None:
        loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        self._thread.join(timeout)

    def _main(self) -> None:
        try:
            asyncio.run(self._serve())
        finally:
            self._ready.set()

    async def _serve(self) -> None:
        self._task = asyncio.current_task()
        with contextlib.suppress(asyncio.CancelledError):
            async with self._open() as collector:
                self._collector = collector
                self._loop = asyncio.get_running_loop()
                if self._sweep:
                    try:
                        collector.startup_sweep()
                    except Exception:
                        log.exception("startup sweep failed")
                self._ready.set()
                await collector.run()
