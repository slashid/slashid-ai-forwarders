from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint

from slashid_codex.cache import SessionCache
from slashid_codex.config import CodexConfig
from slashid_codex.daemon import (
    WATCHDOG_STALE_S,
    Lifetime,
    Services,
    Stopper,
    Watchdog,
    create_app,
    lifetime_of,
    tick,
)
from slashid_codex.discovery import (
    DaemonInfo,
    acquire_lock,
    hmac_response,
    lock_held,
    write_daemon_json,
)
from slashid_codex.emit import Trigger
from slashid_codex.handler import INVALID_PAYLOAD, MAX_VERDICT_S, Handler
from slashid_codex.hooks import PreToolUseHook, UserPromptSubmitHook
from slashid_codex.preflight import Verdict

HOOKS = Path(__file__).parent / "fixtures" / "hooks"
PORT = 47123
SECRET = "k" * 64
AUTH = {"Authorization": f"Bearer {SECRET}"}
BASE = f"http://127.0.0.1:{PORT}"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Fakes:
    def __init__(self) -> None:
        self.preflights: list[tuple[str, bytes]] = []
        self.triggers: list[tuple[str, bytes]] = []
        self.shutdowns = 0
        self.exits: list[int] = []
        self.verdict = Verdict(decision="block", reason="no")
        self.clock = Clock()

    async def preflight(self, event: str, payload: bytes, arrival: float) -> Verdict:
        self.preflights.append((event, payload))
        return self.verdict

    def enqueue(self, event: str, payload: bytes) -> None:
        self.triggers.append((event, payload))

    def shutdown(self) -> None:
        self.shutdowns += 1

    def services(self) -> Services:
        return Services(
            preflight=self.preflight,
            enqueue_trigger=self.enqueue,
            clock=self.clock,
            exit=self.exits.append,
            on_shutdown=self.shutdown,
        )


@pytest.fixture
def fakes() -> Fakes:
    return Fakes()


@pytest.fixture
def config(make_config: Callable[..., CodexConfig]) -> CodexConfig:
    return make_config(daemon_idle_seconds=600)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@pytest.fixture
def app(config: CodexConfig, fakes: Fakes) -> FastAPI:
    return create_app(config, SECRET, PORT, fakes.services())


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(app) as client:
        yield client


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


async def test_ping_answers_hmac_without_auth(client: httpx.AsyncClient) -> None:
    response = await client.get("/ping", params={"nonce": "abc"})
    assert response.status_code == 200
    assert response.text == hmac_response(SECRET, "abc")


async def test_ping_nonce_bounded(client: httpx.AsyncClient) -> None:
    response = await client.get("/ping", params={"nonce": "x" * 129})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": SECRET}]
)
async def test_hooks_need_the_bearer(
    client: httpx.AsyncClient, fakes: Fakes, headers: dict[str, str]
) -> None:
    response = await client.post("/hooks/Stop", content=b"{}", headers=headers)
    assert response.status_code == 401
    assert response.content == b""
    assert (await client.post("/shutdown", headers=headers)).status_code == 401
    assert fakes.triggers == []
    assert fakes.shutdowns == 0


@pytest.mark.parametrize(
    "host", ["localhost:47123", "127.0.0.1", "127.0.0.1:1", "evil.example:47123"]
)
async def test_wrong_host_refused(client: httpx.AsyncClient, host: str) -> None:
    response = await client.get("/ping", params={"nonce": "n"}, headers={"Host": host})
    assert response.status_code == 403
    assert response.content == b""


async def test_origin_refused(client: httpx.AsyncClient, fakes: Fakes) -> None:
    response = await client.post(
        "/hooks/Stop", content=b"{}", headers={**AUTH, "Origin": "http://127.0.0.1:47123"}
    )
    assert response.status_code == 403
    response = await client.get("/ping", params={"nonce": "n"}, headers={"Origin": "null"})
    assert response.status_code == 403
    assert fakes.triggers == []


async def test_bearer_compared_in_constant_time(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import slashid_codex.daemon as daemon

    calls: list[tuple[bytes, bytes]] = []
    real = daemon.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(daemon.hmac, "compare_digest", spy)
    await client.post("/hooks/Stop", content=b"{}", headers={"Authorization": "Bearer x"})
    assert calls == [(b"Bearer x", f"Bearer {SECRET}".encode())]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("event", ["UserPromptSubmit", "PreToolUse"])
async def test_preflight_routes_return_the_verdict(
    client: httpx.AsyncClient, fakes: Fakes, event: str
) -> None:
    response = await client.post(f"/hooks/{event}", content=b"payload", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"decision": "block", "reason": "no"}
    assert fakes.preflights == [(event, b"payload")]
    fakes.verdict = Verdict()
    response = await client.post(f"/hooks/{event}", content=b"p", headers=AUTH)
    assert response.json() == {}


@pytest.mark.parametrize("event", ["Stop", "SessionStart", "SessionEnd"])
async def test_trigger_routes_answer_at_once(
    client: httpx.AsyncClient, fakes: Fakes, event: str
) -> None:
    response = await client.post(f"/hooks/{event}", content=b"payload", headers=AUTH)
    assert response.json() == {}
    assert fakes.triggers == [(event, b"payload")]
    assert fakes.preflights == []


async def test_unknown_event_404(client: httpx.AsyncClient) -> None:
    response = await client.post("/hooks/PostToolUse", content=b"{}", headers=AUTH)
    assert response.status_code == 404


async def test_preflight_crash_answers_fail_mode(
    config: CodexConfig, fakes: Fakes, make_config: Callable[..., CodexConfig]
) -> None:
    async def boom(event: str, payload: bytes, arrival: float) -> Verdict:
        raise RuntimeError("bug")

    for mode, expected in (("deny", "block"), ("allow", None)):
        services = fakes.services()
        services.preflight = boom
        app = create_app(make_config(verdict_fail_mode=mode), SECRET, PORT, services)
        async with _client(app) as client:
            response = await client.post("/hooks/PreToolUse", content=b"{}", headers=AUTH)
        assert Verdict.model_validate_json(response.content).decision == expected


async def test_shutdown_route(client: httpx.AsyncClient, fakes: Fakes) -> None:
    assert (await client.post("/shutdown", headers=AUTH)).json() == {}
    assert fakes.shutdowns == 1


# --------------------------------------------------------------------------
# Lifetime
# --------------------------------------------------------------------------


async def test_idle_clock_reset_by_hooks_and_publishes_only(
    app: FastAPI, client: httpx.AsyncClient, fakes: Fakes
) -> None:
    lifetime = lifetime_of(app)
    fakes.clock.now += 599
    assert not lifetime.idle_expired()
    await client.post("/hooks/Stop", content=b"{}", headers=AUTH)
    fakes.clock.now += 599
    assert not lifetime.idle_expired()
    # Neither a ping nor a refused request counts.
    await client.get("/ping", params={"nonce": "n"})
    await client.post("/hooks/Stop", content=b"{}")
    fakes.clock.now += 1
    assert lifetime.idle_expired()
    # What the collector's ``on_published`` calls.
    lifetime.touch()
    assert not lifetime.idle_expired()
    fakes.clock.now += 600
    assert lifetime.idle_expired()


async def test_tick_calls_on_idle_once() -> None:
    clock = Clock()
    lifetime = Lifetime(clock, 10)
    idle: list[int] = []
    task = asyncio.create_task(tick(lifetime, lambda: idle.append(1), interval=0.01))
    await asyncio.sleep(0.05)
    assert idle == []
    clock.now += 10
    await asyncio.sleep(0.05)
    task.cancel()
    assert idle == [1]
    # It kept beating after going idle.
    assert not lifetime.stale()


async def test_lifespan_exits_when_idle(config: CodexConfig, fakes: Fakes) -> None:
    services = fakes.services()
    app = create_app(config.model_copy(update={"daemon_idle_seconds": 0}), SECRET, PORT, services)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
    assert fakes.shutdowns == 1


def test_watchdog_exits_on_stale_heartbeat(fakes: Fakes) -> None:
    lifetime = Lifetime(fakes.clock, 600)
    watchdog = Watchdog(lifetime, fakes.exits.append)
    fakes.clock.now += WATCHDOG_STALE_S
    assert not watchdog.check()
    lifetime.beat()
    fakes.clock.now += WATCHDOG_STALE_S + 0.1
    assert watchdog.check()
    assert fakes.exits == [1]


def test_watchdog_thread(fakes: Fakes) -> None:
    lifetime = Lifetime(fakes.clock, 600)
    watchdog = Watchdog(lifetime, fakes.exits.append, interval=0.01)
    watchdog.start()
    fakes.clock.now += WATCHDOG_STALE_S + 1
    deadline = time.monotonic() + 5
    while not fakes.exits and time.monotonic() < deadline:
        time.sleep(0.01)
    watchdog.stop()
    assert fakes.exits == [1]


def test_stopper_deletes_json_then_releases_lock(tmp_path: Path) -> None:
    write_daemon_json(tmp_path, DaemonInfo(1, "s", 2, "v", "d"))
    lock = acquire_lock(tmp_path / "daemon.lock", wait=0)
    assert lock is not None
    order: list[str] = []
    real_release = lock.release

    def release() -> None:
        order.append("json gone" if not (tmp_path / "daemon.json").exists() else "json present")
        real_release()

    lock.release = release  # ty: ignore[invalid-assignment]
    stopper = Stopper(tmp_path, lock)
    stopper()
    stopper()
    assert order == ["json gone"]
    assert not lock_held(tmp_path / "daemon.lock")


# --------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------


class FakePreflight:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float | None]] = []

    async def user_prompt_submit(
        self, hook: UserPromptSubmitHook, *, deadline: float | None = None
    ) -> Verdict:
        self.calls.append(("UserPromptSubmit", deadline))
        return Verdict()

    async def pre_tool_use(self, hook: PreToolUseHook, *, deadline: float | None = None) -> Verdict:
        self.calls.append(("PreToolUse", deadline))
        return Verdict(decision="block", reason="r")


class HandlerEnv:
    def __init__(self, config: CodexConfig) -> None:
        self.preflight = FakePreflight()
        self.cache = SessionCache(
            codex_home=config.codex_home, load_watermark=lambda _: Checkpoint(None, None)
        )
        self.submitted: list[Trigger] = []
        self.handler = Handler(config, self.preflight, self.cache, self.submitted.append)
        self.touched: list[str] = []
        real = self.cache.touch_hook

        def touch(session_id: str) -> None:
            self.touched.append(session_id)
            real(session_id)

        self.cache.touch_hook = touch  # ty: ignore[invalid-assignment]


def _payload(name: str) -> bytes:
    return (HOOKS / name).read_bytes()


async def test_handler_preflight_deadline_from_arrival(
    make_config: Callable[..., CodexConfig],
) -> None:
    env = HandlerEnv(make_config(preflight_timeout_seconds=4.0))
    await env.handler.preflight("UserPromptSubmit", _payload("user_prompt_submit.json"), 100.0)
    verdict = await env.handler.preflight(
        "PreToolUse", _payload("pre_tool_use_bash_sed.json"), 200.0
    )
    assert verdict == Verdict(decision="block", reason="r")
    assert env.preflight.calls == [("UserPromptSubmit", 104.0), ("PreToolUse", 204.0)]
    assert env.touched == [
        json.loads(_payload("user_prompt_submit.json"))["session_id"],
        json.loads(_payload("pre_tool_use_bash_sed.json"))["session_id"],
    ]
    capped = HandlerEnv(make_config(preflight_timeout_seconds=30.0))
    await capped.handler.preflight("UserPromptSubmit", _payload("user_prompt_submit.json"), 0.0)
    assert capped.preflight.calls == [("UserPromptSubmit", MAX_VERDICT_S)]


@pytest.mark.parametrize("mode", ["deny", "allow"])
@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("UserPromptSubmit", b"not json"),
        ("UserPromptSubmit", b'{"session_id": "s"}'),
        # A payload for another event.
        ("PreToolUse", (HOOKS / "user_prompt_submit.json").read_bytes()),
    ],
)
async def test_handler_invalid_payload_fail_mode(
    make_config: Callable[..., CodexConfig], mode: str, event: str, payload: bytes
) -> None:
    env = HandlerEnv(make_config(verdict_fail_mode=mode))
    verdict = await env.handler.preflight(event, payload, 0.0)
    if mode == "deny":
        assert verdict == Verdict(decision="block", reason=INVALID_PAYLOAD)
    else:
        assert verdict == Verdict()
    assert env.preflight.calls == []


async def test_handler_preflight_exception_fail_mode(
    make_config: Callable[..., CodexConfig],
) -> None:
    env = HandlerEnv(make_config())

    async def boom(hook: PreToolUseHook, *, deadline: float | None = None) -> Verdict:
        raise RuntimeError("bug")

    env.preflight.pre_tool_use = boom  # ty: ignore[invalid-assignment]
    verdict = await env.handler.preflight("PreToolUse", _payload("pre_tool_use_bash_sed.json"))
    assert verdict.decision == "block"


@pytest.mark.parametrize(
    ("event", "name", "ended"),
    [
        ("Stop", "stop.json", False),
        ("SessionStart", "session_start_startup.json", False),
        ("SessionEnd", "session_end.json", True),
        ("SessionEnd", "session_end_no_transcript.json", True),
    ],
)
def test_handler_trigger(
    make_config: Callable[..., CodexConfig], event: str, name: str, ended: bool
) -> None:
    env = HandlerEnv(make_config())
    env.handler.trigger(event, _payload(name))
    [trigger] = env.submitted
    session_id = json.loads(_payload(name))["session_id"]
    assert trigger.session_id == session_id
    assert trigger.ended is ended
    assert env.touched == [session_id]


def test_handler_invalid_trigger_ignored(make_config: Callable[..., CodexConfig]) -> None:
    env = HandlerEnv(make_config())
    env.handler.trigger("Stop", b"{")
    env.handler.trigger("Stop", _payload("session_end.json"))
    assert env.submitted == []
    assert env.touched == []
