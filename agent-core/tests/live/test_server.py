"""Opt-in checks of the real SlashID preflight endpoint (spec 6, first bullet).

Run with ``SLASHID_LIVE=1 SLASHID_PUSH_TOKEN_FILE=<file> uv run pytest tests/live -s``. The
endpoint is ``SLASHID_ENDPOINT`` (else the default), the budget ``SLASHID_VERDICT_BUDGET_MS`` (else
the Lambda's). Ids are synthetic; the server records every preflight it receives.

Each test prints the HTTP statuses, the outcome kind, the number of deny reasons (never their text:
a reason can name files) and the wall-clock latency. What it asserts is only what is unambiguous:
the call ends in a verdict or in a named failure cause. Whatever the server does is reported as it
is.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from cachetools import TTLCache
from pydantic import JsonValue
from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from helpers import FakeRoles, Load, ctx, denied_text, obj
from slashid_agentcore_forwarder.checks import Checker, Outcome
from slashid_agentcore_forwarder.config import Config
from slashid_agentcore_forwarder.deny import derive_key, is_own_deny
from slashid_agentcore_forwarder.handler import dispatch
from slashid_agentcore_forwarder.harness import cache_key
from slashid_agentcore_forwarder.runtime import Runtime
from slashid_agentcore_forwarder.tool_call import CachedInput

pytestmark = pytest.mark.live

HARNESS = "arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/live-test-harness-EXAMPLE"
HARNESS_ROLE = "arn:aws:iam::123456789012:role/live-test-harness-role-EXAMPLE"
GATEWAY = "arn:aws:bedrock-agentcore:us-east-2:123456789012:gateway/live-test-gateway-EXAMPLE"
GATEWAY_ROLE = "arn:aws:iam::123456789012:role/live-test-gateway-role-EXAMPLE"
SESSION = "00000000-0000-4000-8000-000000000001"
TOOL_USE = "tooluse_EXAMPLE0001"
LIVE_RID = "11111111-1111-4111-8111-1111111111aa"
READ = "gdrive___read_file_content"


@dataclass
class Trip:
    """What one dispatch did on the wire."""

    outcome: Outcome | None = None
    latency_s: float = 0.0
    statuses: list[int] = field(default_factory=list)
    invocation: AIInvocationObservedV1 | None = None


class Recording:
    """The real checker, noting its invocation, outcome and latency."""

    def __init__(self, inner: Checker, trip: Trip) -> None:
        self._inner = inner
        self._trip = trip

    async def check(self, invocation: AIInvocationObservedV1, *, started: float) -> Outcome:
        self._trip.invocation = invocation
        begun = time.monotonic()
        outcome = await self._inner.check(invocation, started=started)
        self._trip.latency_s = time.monotonic() - begun
        self._trip.outcome = outcome
        return outcome


def _token() -> str:
    path = os.environ.get("SLASHID_PUSH_TOKEN_FILE")
    if not path:
        pytest.fail("SLASHID_LIVE=1 needs SLASHID_PUSH_TOKEN_FILE, the file holding the push token")
    try:
        token = Path(path).read_text().strip()
    except OSError as exc:
        pytest.fail(f"cannot read the push token file: {type(exc).__name__}")
    if not token:
        pytest.fail("the push token file is empty")
    return token


def _config() -> Config:
    settings: dict[str, object] = {"push_token": _token()}
    if endpoint := os.environ.get("SLASHID_ENDPOINT"):
        settings["endpoint"] = endpoint
    if budget := os.environ.get("SLASHID_VERDICT_BUDGET_MS"):
        settings["verdict_budget_ms"] = int(budget)
    return Config.model_validate(settings)


@dataclass
class RunResult:
    answer: JsonValue
    trip: Trip
    runtime: Runtime


class Live:
    """A runtime over the real server, and the way to send a payload through ``dispatch``."""

    def __init__(self, config: Config) -> None:
        self.config = config

    async def send(
        self,
        payload: dict[str, JsonValue],
        *,
        roles: FakeRoles,
        cached: CachedInput | None = None,
        gateway: bool = False,
    ) -> RunResult:
        trip = Trip()

        async def note(response: httpx.Response) -> None:
            trip.statuses.append(response.status_code)

        def make_client() -> httpx.AsyncClient:
            return httpx.AsyncClient(event_hooks={"response": [note]})

        checker = Checker(self.config, make_client=make_client)
        runtime = Runtime(
            config=self.config,
            key=derive_key(self.config.push_token),
            checker=Recording(checker, trip),
            roles=roles,
            inputs=TTLCache(10, 600),
        )
        if cached is not None:
            runtime.inputs[cache_key(HARNESS, SESSION, TOOL_USE)] = cached
        client_ctx = ctx(GATEWAY_ARN=GATEWAY, REQUEST_ID=LIVE_RID) if gateway else ctx()
        answer = await dispatch(runtime, payload, client_ctx)
        return RunResult(answer, trip, runtime)


@pytest.fixture
async def live() -> AsyncIterator[Live]:
    yield Live(_config())


def report(label: str, result: RunResult) -> None:
    """One line: statuses, outcome and latency, with a count of reasons and never their text."""
    trip = result.trip
    outcome = trip.outcome
    kind = outcome.kind if outcome else "not-checked"
    detail = ""
    if outcome and outcome.kind == "deny":
        detail = f" deny_reasons={len(outcome.reasons)}"
    elif outcome and outcome.kind == "no_answer":
        detail = f" cause={outcome.why}"
    print(
        f"\n[live] {label}: http={trip.statuses or 'none'} outcome={kind}{detail} "
        f"latency_ms={trip.latency_s * 1000:.0f}"
    )


def assert_answered(result: RunResult) -> Outcome:
    """The check ran and ended in a verdict or in a named cause."""
    outcome = result.trip.outcome
    assert outcome is not None, "no check was sent"
    assert outcome.kind in ("allow", "deny", "no_answer")
    if outcome.kind == "no_answer":
        assert outcome.why in ("transport", "timeout", "status", "unparseable", "budget", "error")
    return outcome


def harness_roles() -> FakeRoles:
    return FakeRoles(role=HARNESS_ROLE)


def with_harness(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    payload["harnessArn"] = HARNESS
    return payload


async def test_harness_call_check_with_connector_ref(live: Live, load: Load) -> None:
    payload = with_harness(load("harness_before_tool_call"))
    result = await live.send(payload, roles=harness_roles())
    report("harness call check, google/ID1", result)
    assert_answered(result)
    event = result.trip.invocation
    assert event is not None
    assert [f.file_refs for f in event.accessed_files or []] == [["google/ID1"]]
    assert event.used_tools is None


async def test_harness_result_check_has_used_tools_and_no_event_type(
    live: Live, load: Load
) -> None:
    payload = with_harness(load("harness_after_tool_call"))
    cached = CachedInput(tool_name=READ, tool_input={"fileId": "ID1"})
    result = await live.send(payload, roles=harness_roles(), cached=cached)
    report("harness result check", result)
    assert_answered(result)
    event = result.trip.invocation
    assert event is not None
    assert [u.tool_use_id for u in event.used_tools or []] == [TOOL_USE]
    assert "event_type" not in event.model_dump(mode="json", exclude_none=True)


async def test_gateway_only_identity(live: Live, load: Load) -> None:
    payload = load("gateway_request")
    request = obj(obj(payload["mcp"])["gatewayRequest"])
    request["context"] = {}  # no caller principal, as on a JWT Gateway
    result = await live.send(payload, roles=FakeRoles(gateway=None), gateway=True)
    report("gateway request, gateway_arn only", result)
    assert_answered(result)
    event = result.trip.invocation
    assert event is not None
    identity = event.identity_details
    assert identity is not None
    assert getattr(identity, "gateway_arn", None) == GATEWAY
    assert getattr(identity, "principal_arn", None) is None


async def test_harness_id_with_principal_arn(live: Live, load: Load) -> None:
    payload = with_harness(load("harness_before_tool_call"))
    result = await live.send(payload, roles=harness_roles())
    report("harness call check, harness id + principal_arn", result)
    assert_answered(result)
    event = result.trip.invocation
    assert event is not None
    identity = event.identity_details
    assert event.used_agent_ids == [HARNESS]
    assert getattr(identity, "principal_arn", None) == HARNESS_ROLE


async def test_deny_round_trip(live: Live, load: Load) -> None:
    """If the server denies, each surface answers with a deny text that is our own."""
    harness = await live.send(with_harness(load("harness_before_tool_call")), roles=harness_roles())
    report("deny round trip, harness", harness)
    gateway = await live.send(load("gateway_request"), roles=FakeRoles(), gateway=True)
    report("deny round trip, gateway", gateway)
    key = derive_key(live.config.push_token)
    for result in (harness, gateway):
        outcome = assert_answered(result)
        if outcome.kind != "deny":
            continue
        answer = result.answer
        text = obj(answer)["reason"] if result is harness else denied_text(answer)
        assert isinstance(text, str)
        if not live.config.shadow_mode:
            assert is_own_deny(text, key)
