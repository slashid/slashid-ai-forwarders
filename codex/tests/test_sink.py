from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx
import pytest
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.sink import PreflightError

from slashid_codex import sink as sink_module
from slashid_codex.config import CodexConfig
from slashid_codex.sink import CodexSink

ConfigFactory = Callable[..., CodexConfig]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    calls: list[float] = []

    async def _sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(sink_module.asyncio, "sleep", _sleep)
    return calls


def _fail(exc: Exception) -> PreflightError:
    try:
        raise PreflightError(repr(exc)) from exc
    except PreflightError as err:
        return err


async def test_dry_run_preflight_logs_and_allows(
    make_config: ConfigFactory,
    invocation: AIInvocationObservedV1,
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _never(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("dry run must not call SlashID")

    monkeypatch.setattr(sink_module, "preflight_invocation", _never)
    caplog.set_level(logging.INFO, logger="slashid_codex.sink")
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(dry_run=True), client)
        assert await sink.preflight(invocation, deadline=time.monotonic() + 4) == []
    assert sleeps == [1.0]
    assert invocation.model_dump_json(exclude_none=True) in caplog.text


async def test_dry_run_push_logs_and_counts(
    make_config: ConfigFactory,
    invocation: AIInvocationObservedV1,
    sleeps: list[float],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _never(*args: object, **kwargs: object) -> int:
        raise AssertionError("dry run must not call SlashID")

    monkeypatch.setattr(sink_module, "push_invocations", _never)
    caplog.set_level(logging.INFO, logger="slashid_codex.sink")
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(dry_run=True), client)
        assert await sink.push([invocation, invocation]) == 2
    assert sleeps == [1.0]
    assert invocation.request_id in caplog.text


async def test_preflight_passes_remaining_budget(
    make_config: ConfigFactory, invocation: AIInvocationObservedV1, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    async def _preflight(client: httpx.AsyncClient, inv: AIInvocationObservedV1, **kw: object):
        seen.append(kw)
        return ["denied by rule"]

    monkeypatch.setattr(sink_module, "preflight_invocation", _preflight)
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(), client)
        reasons = await sink.preflight(invocation, deadline=time.monotonic() + 3)
    assert reasons == ["denied by rule"]
    assert seen[0]["endpoint"] == "https://api.example.test"
    assert seen[0]["push_token"] == "t" * 32
    timeout = seen[0]["timeout_s"]
    assert isinstance(timeout, float)
    assert 2.5 < timeout <= 3


async def test_preflight_retries_once_on_connection_error(
    make_config: ConfigFactory, invocation: AIInvocationObservedV1, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[float] = []

    async def _preflight(client: httpx.AsyncClient, inv: AIInvocationObservedV1, **kw: float):
        calls.append(kw["timeout_s"])
        if len(calls) == 1:
            raise _fail(httpx.RemoteProtocolError("Server disconnected"))
        return []

    monkeypatch.setattr(sink_module, "preflight_invocation", _preflight)
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(), client)
        assert await sink.preflight(invocation, deadline=time.monotonic() + 3) == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        PreflightError("HTTP 503"),
        _fail(httpx.ReadTimeout("slow")),
    ],
)
async def test_preflight_no_retry_on_other_errors(
    make_config: ConfigFactory,
    invocation: AIInvocationObservedV1,
    monkeypatch: pytest.MonkeyPatch,
    error: PreflightError,
) -> None:
    calls: list[int] = []

    async def _preflight(client: httpx.AsyncClient, inv: AIInvocationObservedV1, **kw: object):
        calls.append(1)
        raise error

    monkeypatch.setattr(sink_module, "preflight_invocation", _preflight)
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(), client)
        with pytest.raises(PreflightError):
            await sink.preflight(invocation, deadline=time.monotonic() + 3)
    assert calls == [1]


async def test_preflight_no_retry_past_deadline(
    make_config: ConfigFactory, invocation: AIInvocationObservedV1, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    deadline = time.monotonic() + 3

    async def _preflight(client: httpx.AsyncClient, inv: AIInvocationObservedV1, **kw: object):
        calls.append(1)
        monkeypatch.setattr(sink_module.time, "monotonic", lambda: deadline + 1)
        raise _fail(httpx.ConnectError("refused"))

    monkeypatch.setattr(sink_module, "preflight_invocation", _preflight)
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(), client)
        with pytest.raises(PreflightError):
            await sink.preflight(invocation, deadline=deadline)
    assert calls == [1]


async def test_preflight_expired_deadline(
    make_config: ConfigFactory, invocation: AIInvocationObservedV1, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _never(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("no time left")

    monkeypatch.setattr(sink_module, "preflight_invocation", _never)
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(), client)
        with pytest.raises(PreflightError):
            await sink.preflight(invocation, deadline=time.monotonic() - 1)


async def test_push_delegates(
    make_config: ConfigFactory, invocation: AIInvocationObservedV1, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    async def _push(client: httpx.AsyncClient, events: list[AIInvocationObservedV1], **kw: object):
        seen.append(kw)
        return len(events)

    monkeypatch.setattr(sink_module, "push_invocations", _push)
    async with httpx.AsyncClient() as client:
        sink = CodexSink(make_config(max_retries=2), client)
        assert await sink.push([invocation]) == 1
    assert seen == [
        {"endpoint": "https://api.example.test", "push_token": "t" * 32, "max_retries": 2}
    ]
