from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.events import AIAccessedFile, AIInvocationObservedV1
from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint
from slashid_ai_forwarder_core.platform.local import LocalPlatform, create_local_platform

from slashid_codex import emit
from slashid_codex.cache import SessionCache
from slashid_codex.config import CodexConfig
from slashid_codex.cursor import RolloutCursor, RolloutInvocation
from slashid_codex.emit import COLLECTION, Collector, Trigger, Worker
from slashid_codex.log import SessionLog
from slashid_codex.state import SqliteFileRecordStore, connect

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
SESSIONS = {
    "script": "01a0f397-f16e-7d83-87e7-6701f1b384c7",
    "function": "01a0f44f-53e8-7283-b1e5-b74b1da1b89d",
    "interrupt": "01a0f38b-f3a4-7c70-95e2-420a7fcbcc03",
    "compaction": "01a0f553-7026-70e1-ae0c-d833daddaa9e",
}
ENTRY = AIAccessedFile(name="/f", provenance="tool_result")


def _invocations(name: str) -> list[RolloutInvocation]:
    log = SessionLog.open(ROLLOUTS / f"{name}.jsonl", lambda _: None)
    log.refresh()
    cursor = RolloutCursor(log)
    out = []
    while (invocation := cursor.next_closed()) is not None:
        out.append(invocation)
    return out


class FakeSink:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.pushed: list[list[str]] = []
        self.conversations: list[str | None] = []
        self.fail: Callable[[int], bool] = lambda _: False
        self.lose_response = False
        self.on_push: Callable[[list[AIInvocationObservedV1]], None] = lambda _: None

    async def push(self, events: list[AIInvocationObservedV1]) -> int:
        ids = [e.request_id for e in events]
        self.calls.append(ids)
        self.on_push(events)
        if self.lose_response:
            self.lose_response = False
            self.pushed.append(ids)
            raise RuntimeError("connection reset after send")
        if self.fail(len(self.calls)):
            raise RuntimeError("503")
        self.pushed.append(ids)
        self.conversations += [e.conversation_id for e in events]
        return len(events)


class Env:
    def __init__(
        self,
        tmp_path: Path,
        config: CodexConfig,
        platform: LocalPlatform,
        *,
        created_at: datetime | None = None,
    ) -> None:
        self.config = config
        self.codex_home = config.codex_home
        self.state_dir = tmp_path / "state"
        self.platform = platform
        self.records = SqliteFileRecordStore(lambda: connect(self.state_dir))
        self.cache = SessionCache(codex_home=self.codex_home)
        self.sink = FakeSink()
        self.sleeps: list[float] = []
        self.published = 0

        async def sleep(delay: float) -> None:
            self.sleeps.append(delay)

        def published() -> None:
            self.published += 1

        self.collector = Collector(
            config,
            self.sink,
            self.records,
            self.platform,
            self.cache,
            created_at=created_at or datetime.now(UTC) - timedelta(days=30),
            sleep=sleep,
            on_published=published,
        )

    def store(self, session_id: str):
        return self.platform.checkpoint_store(collection=COLLECTION, document=session_id)

    def rollout(self, name: str, *, root: str = "sessions", age: timedelta | None = None) -> Path:
        folder = self.codex_home / root / "2026" / "09" / "30"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-09-30T18-00-00-{SESSIONS[name]}.jsonl"
        shutil.copy(ROLLOUTS / f"{name}.jsonl", path)
        if age is not None:
            stamp = (datetime.now(UTC) - age).timestamp()
            os.utime(path, (stamp, stamp))
        return path

    def session(self, name: str):
        return self.cache.get(SESSIONS[name], self.rollout(name))


@asynccontextmanager
async def open_env(
    tmp_path: Path, config: CodexConfig, *, created_at: datetime | None = None
) -> AsyncIterator[Env]:
    async with create_local_platform(tmp_path / "state") as platform:
        yield Env(tmp_path, config, platform, created_at=created_at)


@pytest.fixture
async def env(tmp_path: Path, make_config: Callable[..., CodexConfig]) -> AsyncIterator[Env]:
    async with open_env(tmp_path, make_config()) as env:
        yield env


def _flat(batches: list[list[str]]) -> list[str]:
    return [i for batch in batches for i in batch]


# --------------------------------------------------------------------------
# process
# --------------------------------------------------------------------------


async def test_process_sends_and_saves(env: Env) -> None:
    invocations = _invocations("function")
    sid = SESSIONS["function"]
    [sed] = invocations[2].consumed_items
    assert sed.id is not None
    env.records.put_turn(sid, invocations[0].turn_id, [ENTRY])
    env.records.put_call(sid, invocations[1].turn_id, sed.id, ENTRY)
    # Never consumed: a denied call in turn 2 (finished before response 4),
    # and one in the last turn, whose end no sent response follows.
    env.records.put_call(sid, invocations[1].turn_id, "call_denied", ENTRY)
    env.records.put_call(sid, invocations[4].turn_id, "call_open", ENTRY)

    assert await env.collector.process(env.session("function"))

    assert env.sink.pushed == [[i.response_id for i in invocations]]
    last = invocations[-1]
    assert await env.store(sid).load() == Checkpoint(last.timestamp, last.response_id)
    assert env.records.for_round(sid, [invocations[0].turn_id], []) == []
    assert env.records.for_round(sid, [], [sed.id, "call_denied"]) == []
    assert env.records.for_round(sid, [], ["call_open"]) == [ENTRY]
    assert env.published == 1


async def test_watermark_per_batch(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emit, "BATCH_SIZE", 2)
    invocations = _invocations("function")
    sid = SESSIONS["function"]
    seen: list[Checkpoint | None] = []
    # The in-memory watermark, which tracks what was saved.
    session = env.session("function")
    env.sink.on_push = lambda _: seen.append(session.watermark)

    await env.collector.process(session)

    assert [len(b) for b in env.sink.pushed] == [2, 2, 1]
    assert seen == [
        Checkpoint(None, None),
        Checkpoint(invocations[1].timestamp, invocations[1].response_id),
        Checkpoint(invocations[3].timestamp, invocations[3].response_id),
    ]
    assert env.published == 3
    last = invocations[-1]
    assert await env.store(sid).load() == Checkpoint(last.timestamp, last.response_id)


async def test_failing_batch_retried(env: Env) -> None:
    env.sink.fail = lambda call: call <= 2
    assert await env.collector.process(env.session("script"))
    assert env.sleeps == [1.0, 5.0]
    assert len(env.sink.calls) == 3
    assert env.sink.pushed == [[i.response_id for i in _invocations("script")]]


async def test_failed_batch_blocks_later_ones(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emit, "BATCH_SIZE", 2)
    invocations = _invocations("function")
    sid = SESSIONS["function"]
    env.sink.fail = lambda call: call > 1
    session = env.session("function")

    assert not await env.collector.process(session)

    assert _flat(env.sink.pushed) == [i.response_id for i in invocations[:2]]
    assert env.sleeps == list(emit.RETRY_DELAYS_S)
    assert (await env.store(sid).load()).id == invocations[1].response_id
    assert env.published == 1
    # The cursor is back at the watermark: the failed batch comes next.
    nxt = session.send.next_closed()
    assert nxt is not None
    assert nxt.response_id == invocations[2].response_id
    assert not session.batch_in_flight


async def test_repeat_after_lost_response(env: Env) -> None:
    env.sink.lose_response = True
    assert await env.collector.process(env.session("script"))
    ids = [i.response_id for i in _invocations("script")]
    assert env.sink.pushed == [ids, ids]
    assert (await env.store(SESSIONS["script"]).load()).id == ids[-1]


async def test_resume_from_watermark(env: Env) -> None:
    invocations = _invocations("function")
    await env.store(SESSIONS["function"]).save(
        Checkpoint(invocations[2].timestamp, invocations[2].response_id)
    )
    await env.collector.process(env.session("function"))
    assert env.sink.pushed == [[i.response_id for i in invocations[3:]]]


class FailingMcp:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self) -> list:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("mcp listing broke")
        return []


async def test_escaping_error_rewinds(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emit, "BATCH_SIZE", 2)
    invocations = _invocations("function")
    sid = SESSIONS["function"]
    session = env.session("function")
    assert await env.collector.process(session) is True
    env.sink.pushed.clear()
    # The session again from scratch, with a listing that fails once.
    env.cache._sessions.clear()
    await env.store(sid).save(Checkpoint(invocations[1].timestamp, invocations[1].response_id))
    env.collector._mcp = FailingMcp()  # ty: ignore[invalid-assignment]
    session = env.session("function")

    with pytest.raises(RuntimeError):
        await env.collector.process(session)
    assert not session.batch_in_flight

    env.collector.trigger(Trigger(sid, None))
    await env.collector.drain()
    assert _flat(env.sink.pushed) == [i.response_id for i in invocations[2:]]


async def test_publish_hook_error_keeps_watermark(env: Env) -> None:
    invocations = _invocations("script")
    session = env.session("script")

    def boom() -> None:
        raise RuntimeError("hook")

    env.collector._on_published = boom
    with pytest.raises(RuntimeError):
        await env.collector.process(session)
    last = invocations[-1]
    assert session.watermark == Checkpoint(last.timestamp, last.response_id)
    assert session.send.next_closed() is None


async def test_unbuildable_response_dropped_quietly(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    invocations = _invocations("function")
    bad = invocations[1].response_id
    real = emit.build_event

    async def build(invocation: RolloutInvocation, *args: object, **kwargs: object):
        if invocation.response_id == bad:
            raise ValueError("secret prompt text")
        return await real(invocation, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(emit, "build_event", build)
    with caplog.at_level(logging.DEBUG):
        assert await env.collector.process(env.session("function"))

    assert env.sink.pushed == [[i.response_id for i in invocations if i.response_id != bad]]
    assert "secret prompt text" not in caplog.text
    [record] = [r for r in caplog.records if bad in r.getMessage()]
    assert "ValueError" in record.getMessage()
    assert record.exc_info is None


async def test_batch_of_unbuildable_responses_fails(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def build(*_args: object, **_kwargs: object):
        raise ValueError("secret prompt text")

    monkeypatch.setattr(emit, "build_event", build)
    session = env.session("script")
    with caplog.at_level(logging.DEBUG):
        assert not await env.collector.process(session)

    assert env.sink.calls == []
    assert env.sleeps == list(emit.RETRY_DELAYS_S)
    assert "secret prompt text" not in caplog.text
    assert await env.store(SESSIONS["script"]).load() == Checkpoint(None, None)
    nxt = session.send.next_closed()
    assert nxt is not None
    assert nxt.response_id == _invocations("script")[0].response_id


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------


async def test_collect_locates_and_ends(env: Env) -> None:
    env.rollout("script", root="archived_sessions")
    env.collector.trigger(Trigger(SESSIONS["script"], None, ended=True))
    await env.collector.drain()
    assert len(_flat(env.sink.pushed)) == 2
    # Ended and fully sent: evicted.
    assert env.cache.evict() == []
    assert SESSIONS["script"] not in env.cache._sessions


async def test_collect_missing_rollout_ignored(env: Env) -> None:
    env.collector.trigger(Trigger("nope", None, ended=True))
    await env.collector.drain()
    assert env.sink.calls == []


# --------------------------------------------------------------------------
# Startup sweep
# --------------------------------------------------------------------------


async def test_sweep_order_and_bounds(env: Env) -> None:
    env.rollout("script", age=timedelta(hours=1))
    env.rollout("function", root="archived_sessions", age=timedelta(hours=2))
    env.rollout("compaction", age=timedelta(hours=3))
    env.rollout("interrupt", age=timedelta(days=8))
    invocations = _invocations("script")

    def overtake(events: list[AIInvocationObservedV1]) -> None:
        if events[0].conversation_id == SESSIONS["script"]:
            env.collector.trigger(Trigger(SESSIONS["interrupt"], None))

    env.sink.on_push = overtake
    assert await env.collector.startup_sweep() == 3
    await env.collector.drain()

    assert list(dict.fromkeys(env.sink.conversations)) == [
        SESSIONS["script"],
        SESSIONS["interrupt"],
        SESSIONS["function"],
        SESSIONS["compaction"],
    ]
    assert (await env.store(SESSIONS["script"]).load()).id == invocations[-1].response_id
    # Sweep sessions are not started: evicted once sent.
    assert env.cache._sessions == {}


async def test_sweep_skips_before_created_at(
    tmp_path: Path, make_config: Callable[..., CodexConfig]
) -> None:
    created = datetime.now(UTC) - timedelta(minutes=30)
    async with open_env(tmp_path, make_config(), created_at=created) as env:
        env.rollout("script", age=timedelta(hours=1))
        env.rollout("function", age=timedelta(minutes=1))
        assert await env.collector.startup_sweep() == 1


async def test_sweep_skips_unmodified_since_watermark(env: Env) -> None:
    env.rollout("script", age=timedelta(hours=1))
    await env.store(SESSIONS["script"]).save(
        Checkpoint(datetime.now(UTC) - timedelta(minutes=59), "x")
    )
    env.rollout("function", age=timedelta(hours=1))
    await env.store(SESSIONS["function"]).save(
        Checkpoint(datetime.now(UTC) - timedelta(hours=2), "x")
    )
    assert await env.collector.startup_sweep() == 1


async def test_sweep_prunes(env: Env) -> None:
    old = datetime.now(UTC) - timedelta(days=8)
    SqliteFileRecordStore(lambda: connect(env.state_dir), clock=lambda: old).put_call(
        "s", "t", "c", ENTRY
    )
    env.records.put_call("s", "t", "fresh", ENTRY)
    await env.store("old").save(Checkpoint(old, "r"))
    await env.store("new").save(Checkpoint(datetime.now(UTC), "r"))
    other = env.platform.checkpoint_store(collection="other", document="old")
    await other.save(Checkpoint(old, "r"))

    await env.collector.startup_sweep()

    assert env.records.for_round("s", [], ["c", "fresh"]) == [ENTRY]
    assert await env.store("old").load() == Checkpoint(None, None)
    assert (await env.store("new").load()).id == "r"
    assert (await other.load()).id == "r"


async def test_watermark_never_moves_back(env: Env) -> None:
    invocations = _invocations("function")
    sid = SESSIONS["function"]
    ahead = Checkpoint(invocations[-1].timestamp + timedelta(seconds=1), "later")
    session = env.session("function")
    session.rewind_send(Checkpoint(None, None))
    session.watermark = ahead

    assert await env.collector.process(session)

    assert env.sink.pushed == [[i.response_id for i in invocations]]
    assert await env.store(sid).load() == Checkpoint(None, None)
    assert session.watermark == ahead


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------


def test_worker_thread(tmp_path: Path, make_config: Callable[..., CodexConfig]) -> None:
    config = make_config()
    done = threading.Event()
    holder: dict[str, Env] = {}

    @asynccontextmanager
    async def open_collector() -> AsyncIterator[Collector]:
        assert threading.current_thread().name == "codex-collector"
        async with open_env(tmp_path, config) as env:
            env.sink.on_push = lambda _: done.set()
            holder["env"] = env
            yield env.collector

    worker = Worker(open_collector, sweep=False)
    worker.start()
    env = holder["env"]
    env.rollout("script")
    worker.submit(Trigger(SESSIONS["script"], None))
    assert done.wait(5)
    worker.stop()
    assert len(env.sink.pushed) == 1


def test_worker_ready_before_sweep(
    tmp_path: Path, make_config: Callable[..., CodexConfig], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config()
    swept = threading.Event()

    @asynccontextmanager
    async def open_collector() -> AsyncIterator[Collector]:
        async with open_env(tmp_path, config) as env:

            async def slow_sweep() -> int:
                time.sleep(0.5)
                swept.set()
                return 0

            monkeypatch.setattr(env.collector, "startup_sweep", slow_sweep)
            yield env.collector

    worker = Worker(open_collector)
    start = time.monotonic()
    worker.start()
    assert time.monotonic() - start < 0.4
    assert swept.wait(5)
    worker.stop()


def test_worker_setup_failure_exits(caplog: pytest.LogCaptureFixture) -> None:
    exited = threading.Event()

    @asynccontextmanager
    async def open_collector() -> AsyncIterator[Collector]:
        raise OSError("disk full")
        yield

    worker = Worker(open_collector, on_fatal=exited.set)
    with caplog.at_level(logging.ERROR):
        worker.start()
        assert exited.wait(5)
        worker.stop()
    assert any(r.levelno == logging.ERROR and r.name == emit.__name__ for r in caplog.records)
