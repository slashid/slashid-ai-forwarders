from __future__ import annotations

import os
import shutil
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.events import AIAccessedFile, AIInvocationObservedV1
from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint

from slashid_codex import emit
from slashid_codex.cache import SessionCache
from slashid_codex.config import CodexConfig
from slashid_codex.cursor import RolloutCursor, RolloutInvocation
from slashid_codex.dev_platform import DevPlatform
from slashid_codex.emit import COLLECTION, Collector, Trigger, Worker
from slashid_codex.log import SessionLog
from slashid_codex.state import SqliteFileRecordStore

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
SESSIONS = {
    "script": "01a0f397-f16e-7d83-87e7-6701f1b384c7",
    "function": "01a0f44f-53e8-7283-b1e5-b74b1da1b89d",
    "interrupt": "01a0f392-6406-7d82-8234-af07c8203a7c",
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
    def __init__(self, tmp_path: Path, config: CodexConfig) -> None:
        self.config = config
        self.codex_home = config.codex_home
        self.platform = DevPlatform(tmp_path / "state")
        self.records = SqliteFileRecordStore(self.platform.connect)
        self.cache = SessionCache(
            codex_home=self.codex_home,
            load_watermark=lambda sid: self.store(sid).load(),
        )
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


@pytest.fixture
def env(tmp_path: Path, make_config: Callable[..., CodexConfig]) -> Env:
    return Env(tmp_path, make_config())


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
    assert env.store(sid).load() == Checkpoint(last.timestamp, last.response_id)
    assert env.records.for_round(sid, [invocations[0].turn_id], []) == []
    assert env.records.for_round(sid, [], [sed.id, "call_denied"]) == []
    assert env.records.for_round(sid, [], ["call_open"]) == [ENTRY]
    assert env.published == 1


async def test_watermark_per_batch(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(emit, "BATCH_SIZE", 2)
    invocations = _invocations("function")
    sid = SESSIONS["function"]
    seen: list[Checkpoint] = []
    env.sink.on_push = lambda _: seen.append(env.store(sid).load())

    await env.collector.process(env.session("function"))

    assert [len(b) for b in env.sink.pushed] == [2, 2, 1]
    assert seen == [
        Checkpoint(None, None),
        Checkpoint(invocations[1].timestamp, invocations[1].response_id),
        Checkpoint(invocations[3].timestamp, invocations[3].response_id),
    ]
    assert env.published == 3


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
    assert env.store(sid).load().id == invocations[1].response_id
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
    assert env.store(SESSIONS["script"]).load().id == ids[-1]


async def test_resume_from_watermark(env: Env) -> None:
    invocations = _invocations("function")
    env.store(SESSIONS["function"]).save(
        Checkpoint(invocations[2].timestamp, invocations[2].response_id)
    )
    await env.collector.process(env.session("function"))
    assert env.sink.pushed == [[i.response_id for i in invocations[3:]]]


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


def _created_at(env: Env, when: datetime) -> None:
    state = env.platform._state_dir
    state.mkdir(parents=True, exist_ok=True)
    (state / "created_at").write_text(when.isoformat())


async def test_sweep_order_and_bounds(env: Env) -> None:
    _created_at(env, datetime.now(UTC) - timedelta(days=30))
    env.rollout("script", age=timedelta(hours=1))
    env.rollout("function", root="archived_sessions", age=timedelta(hours=2))
    env.rollout("compaction", age=timedelta(hours=3))
    env.rollout("interrupt", age=timedelta(days=8))
    invocations = _invocations("script")

    def overtake(events: list[AIInvocationObservedV1]) -> None:
        if events[0].conversation_id == SESSIONS["script"]:
            env.collector.trigger(Trigger(SESSIONS["interrupt"], None))

    env.sink.on_push = overtake
    assert env.collector.startup_sweep() == 3
    await env.collector.drain()

    assert list(dict.fromkeys(env.sink.conversations)) == [
        SESSIONS["script"],
        SESSIONS["interrupt"],
        SESSIONS["function"],
        SESSIONS["compaction"],
    ]
    assert env.store(SESSIONS["script"]).load().id == invocations[-1].response_id
    # Sweep sessions are not started: evicted once sent.
    assert env.cache._sessions == {}


async def test_sweep_skips_before_created_at(env: Env) -> None:
    _created_at(env, datetime.now(UTC) - timedelta(minutes=30))
    env.rollout("script", age=timedelta(hours=1))
    env.rollout("function", age=timedelta(minutes=1))
    assert env.collector.startup_sweep() == 1


async def test_sweep_skips_unmodified_since_watermark(env: Env) -> None:
    _created_at(env, datetime.now(UTC) - timedelta(days=30))
    env.rollout("script", age=timedelta(hours=1))
    env.store(SESSIONS["script"]).save(Checkpoint(datetime.now(UTC) - timedelta(minutes=59), "x"))
    env.rollout("function", age=timedelta(hours=1))
    env.store(SESSIONS["function"]).save(Checkpoint(datetime.now(UTC) - timedelta(hours=2), "x"))
    assert env.collector.startup_sweep() == 1


async def test_sweep_prunes(tmp_path: Path, make_config: Callable[..., CodexConfig]) -> None:
    env = Env(tmp_path, make_config())
    old = datetime.now(UTC) - timedelta(days=8)
    SqliteFileRecordStore(env.platform.connect, clock=lambda: old).put_call("s", "t", "c", ENTRY)
    env.records.put_call("s", "t", "fresh", ENTRY)
    env.store("old").save(Checkpoint(old, "r"))
    env.store("new").save(Checkpoint(datetime.now(UTC), "r"))

    env.collector.startup_sweep()

    assert env.records.for_round("s", [], ["c", "fresh"]) == [ENTRY]
    assert env.store("old").load() == Checkpoint(None, None)
    assert env.store("new").load().id == "r"


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
        env = Env(tmp_path, config)
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
