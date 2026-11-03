import asyncio
import json
import logging

import httpx
import pytest
from slashid_ai_forwarder_core.events import AIInvocationObservedV1, AIModel, AWSIdentityDetails

from slashid_agentcore_forwarder.checks import (
    POLICY_UNAVAILABLE,
    UNREADABLE,
    Checker,
    Decision,
    Outcome,
    decide,
    unreadable,
)
from slashid_agentcore_forwarder.config import Config
from slashid_agentcore_forwarder.deny import derive_key, is_own_deny
from slashid_agentcore_forwarder.logs import FailReason

KEY = derive_key("push-token")
LOGGER = "slashid_agentcore_forwarder"


def make_config(**overrides: object) -> Config:
    return Config.model_validate(
        {"endpoint": "http://test", "push_token": "push-token", **overrides}
    )


INVOCATION = AIInvocationObservedV1(
    request_id="r1",
    timestamp="2026-11-03T12:00:00+00:00",
    identity_details=AWSIdentityDetails(
        gateway_arn="arn:aws:bedrock-agentcore:us-east-2:1:gateway/g"
    ),
    model=AIModel(id="unknown"),
    parsed_as="agentcore-gateway-hook",
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """A Checker over scripted responses; records requests and the clients it made."""

    def __init__(self, *script: httpx.Response | Exception, config: Config | None = None) -> None:
        self.script = list(script)
        self.requests: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []
        self.clock = Clock()
        self.config = config or make_config()
        self.checker = Checker(
            self.config, clock=self.clock, monotonic=self.clock, make_client=self._make
        )

    def _make(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.clients.append(client)
        return client

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            self.clock.now += 1
            raise step
        return step

    async def check(self) -> Outcome:
        return await self.checker.check(INVOCATION, started=self.clock.now)


def verdict(reasons: list[str], status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"deny_reasons": reasons})


async def test_an_empty_list_allows() -> None:
    rig = Rig(verdict([]))
    assert await rig.check() == Outcome("allow")
    assert rig.requests[0].url == "http://test/ip/nhi/events/ai-invocations/preflight"
    assert rig.requests[0].headers["authorization"] == "Bearer push-token"


async def test_reasons_deny() -> None:
    assert await Rig(verdict(["no"])).check() == Outcome("deny", ["no"])


@pytest.mark.parametrize(
    ("response", "why"),
    [
        (httpx.Response(500, text="boom"), "status"),
        (httpx.Response(200, text="not json"), "unparseable"),
        (httpx.Response(200, json={"other": 1}), "unparseable"),
    ],
)
async def test_no_verdict_is_a_no_answer(response: httpx.Response, why: FailReason) -> None:
    assert await Rig(response).check() == Outcome("no_answer", why=why)


async def test_a_timeout_is_not_retried() -> None:
    rig = Rig(httpx.ReadTimeout("slow"))
    assert await rig.check() == Outcome("no_answer", why="timeout")
    assert len(rig.requests) == 1


async def test_a_connect_error_is_retried_once_on_a_fresh_client() -> None:
    rig = Rig(httpx.ConnectError("stale"), verdict([]))
    assert await rig.check() == Outcome("allow")
    assert len(rig.requests) == 2 and len(rig.clients) == 2
    first, second = (float(r.headers["slashid-request-timeout"]) for r in rig.requests)
    assert second < first
    assert rig.clients[0].is_closed


async def test_two_transport_errors_are_a_no_answer() -> None:
    rig = Rig(httpx.ConnectError("a"), httpx.ConnectError("b"))
    assert await rig.check() == Outcome("no_answer", why="transport")
    assert len(rig.requests) == 2


async def test_a_spent_budget_sends_nothing() -> None:
    rig = Rig()
    started = rig.clock.now - 3.5
    assert await rig.checker.check(INVOCATION, started=started) == Outcome(
        "no_answer", why="budget"
    )
    assert rig.requests == []


async def test_the_retry_needs_budget_left() -> None:
    rig = Rig(httpx.ConnectError("a"), verdict([]), config=make_config(verdict_budget_ms=1_000))
    # The failed request burns 1 s of the 1 s budget.
    assert await rig.check() == Outcome("no_answer", why="budget")
    assert len(rig.requests) == 1


async def test_the_budget_bounds_a_slow_response() -> None:
    rig = Rig(verdict([]), config=make_config(verdict_budget_ms=100))

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return verdict([])

    rig.checker = Checker(
        rig.config,
        clock=rig.clock,
        monotonic=rig.clock,
        make_client=lambda: httpx.AsyncClient(transport=httpx.MockTransport(slow)),
    )
    assert await rig.check() == Outcome("no_answer", why="timeout")


async def test_an_idle_client_is_replaced() -> None:
    rig = Rig(verdict([]), verdict([]), verdict([]))
    await rig.check()
    rig.clock.now += 3
    await rig.check()
    assert len(rig.clients) == 1
    rig.clock.now += 4.5
    await rig.check()
    assert len(rig.clients) == 2 and rig.clients[0].is_closed


async def test_an_unexpected_exception_is_an_error() -> None:
    rig = Rig(RuntimeError("bug"))
    assert await rig.check() == Outcome("no_answer", why="error")


def lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [json.loads(r.message) for r in caplog.records if r.name == LOGGER]


def run_decide(outcome: Outcome, *, prefix: str = "", **config: object) -> Decision:
    return decide(
        outcome,
        config=make_config(**config),
        key=KEY,
        surface="gateway",
        check="result",
        prefix=prefix,
    )


def test_allow_decides_none() -> None:
    assert run_decide(Outcome("allow")) == Decision(None)


def test_a_deny_carries_the_reasons_and_a_reference() -> None:
    text = run_decide(Outcome("deny", ["Not allowed."])).deny
    assert text is not None and text.startswith("Not allowed. (ref ") and is_own_deny(text, KEY)


def test_a_result_deny_keeps_the_prefix() -> None:
    text = run_decide(Outcome("deny", ["Not allowed."]), prefix="Content withheld: ").deny
    assert text is not None
    assert text.startswith("Content withheld: Not allowed. (ref ") and is_own_deny(text, KEY)


def test_no_answer_under_allow_logs_and_allows(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert run_decide(Outcome("no_answer", why="timeout")) == Decision(None)
    assert lines(caplog) == [
        {
            "event": "fail_mode",
            "surface": "gateway",
            "check": "result",
            "reason": "timeout",
            "mode": "allow",
        }
    ]


def test_no_answer_under_deny_blocks(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    text = run_decide(
        Outcome("no_answer", why="status"), prefix="Content withheld: ", verdict_fail_mode="deny"
    ).deny
    assert text is not None and is_own_deny(text, KEY)
    assert text.startswith(f"Content withheld: {POLICY_UNAVAILABLE} (ref ")
    assert lines(caplog)[0]["mode"] == "deny"


def test_the_policy_unavailable_text() -> None:
    text = run_decide(Outcome("no_answer", why="budget"), verdict_fail_mode="deny").deny
    assert text is not None
    assert text.startswith("Your organization's policy check is unavailable. (ref ")


def test_shadow_turns_a_deny_into_allow(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert run_decide(Outcome("deny", ["no"]), shadow_mode=True) == Decision(None)
    assert lines(caplog) == [
        {
            "event": "shadow",
            "surface": "gateway",
            "check": "result",
            "verdict": "deny",
            "answered": "allow",
        }
    ]


def test_shadow_turns_a_fail_mode_deny_into_allow(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    decision = run_decide(
        Outcome("no_answer", why="timeout"), shadow_mode=True, verdict_fail_mode="deny"
    )
    assert decision == Decision(None)
    assert [line["event"] for line in lines(caplog)] == ["fail_mode", "shadow"]


def test_shadow_leaves_an_allow_alone(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert run_decide(Outcome("allow"), shadow_mode=True) == Decision(None)
    assert lines(caplog) == []


def run_unreadable(*, prefix: str = "", **config: object) -> Decision:
    return unreadable(
        config=make_config(**config),
        key=KEY,
        surface="harness",
        check="call",
        reason="bad_tool_call",
        prefix=prefix,
    )


def test_unreadable_denies_whatever_the_mode(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    text = run_unreadable(verdict_fail_mode="allow", prefix="Content withheld: ").deny
    assert text is not None and is_own_deny(text, KEY)
    assert text.startswith(f"Content withheld: {UNREADABLE} (ref ")
    assert [line["event"] for line in lines(caplog)] == ["unreadable", "fail_mode"]
    assert lines(caplog)[1]["reason"] == "unreadable" and lines(caplog)[1]["mode"] == "deny"


def test_unreadable_allows_in_shadow(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert run_unreadable(shadow_mode=True) == Decision(None)
    assert [line["event"] for line in lines(caplog)] == ["unreadable", "fail_mode", "shadow"]
