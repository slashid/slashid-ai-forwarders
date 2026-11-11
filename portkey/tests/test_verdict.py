"""One event, one answer: SlashID's verdict, or the fail mode when it gave none."""

from __future__ import annotations

from typing import Any

from slashid_ai_forwarder_core.events import (
    AIGatewayIdentityDetails,
    AIInvocationObservedV1,
    AIModel,
)

from slashid_portkey_forwarder.config import Config
from slashid_portkey_forwarder.verdict import decide, unjudged
from tests.slashid import ENDPOINT, SlashID

EVENT = AIInvocationObservedV1(
    request_id="pk_1",
    timestamp="2026-11-10T12:00:00+00:00",
    identity_details=AIGatewayIdentityDetails(user_email="ada@example.com"),
    model=AIModel(id="gpt-5", provider="openai"),
    parsed_as="portkey-openai-chat",
)
BLOCKED = "Prompt matches the blocked pattern 'project falcon'."


def config(**overrides: Any) -> Config:
    settings: dict[str, Any] = {"endpoint": ENDPOINT, **overrides}
    return Config(**settings)


async def test_no_deny_reason_allows_and_the_callers_token_goes_to_slashid() -> None:
    slashid = SlashID()
    decision = await decide(EVENT, token="tok-1", config=config(), client=slashid.client)
    assert decision.answered.to_wire() == {"verdict": True}
    assert slashid.tokens == ["Bearer tok-1"]
    assert [e["request_id"] for e in slashid.checked] == ["pk_1"]


async def test_deny_reasons_deny_and_travel_back_to_the_gateway() -> None:
    slashid = SlashID(BLOCKED, "Model gpt-5 is not allowed.")
    decision = await decide(EVENT, token="tok-1", config=config(), client=slashid.client)
    assert decision.answered.to_wire() == {
        "verdict": False,
        "data": {
            "deny_reasons": [BLOCKED, "Model gpt-5 is not allowed."],
            "explanation": "Prompt matches the blocked pattern 'project falcon'; "
            "Model gpt-5 is not allowed.",
        },
    }


async def test_a_check_that_gave_no_answer_allows_by_default() -> None:
    slashid = SlashID(preflight_status=503)
    decision = await decide(EVENT, token="tok-1", config=config(), client=slashid.client)
    assert decision.answered.to_wire() == {"verdict": True}
    assert decision.answered.source == "fail_mode"


async def test_a_check_that_gave_no_answer_denies_when_the_fail_mode_says_so() -> None:
    slashid = SlashID(timeout=True)
    cfg = config(verdict_fail_mode="deny")
    decision = await decide(EVENT, token="tok-1", config=cfg, client=slashid.client)
    wire = decision.answered.to_wire()
    assert wire["verdict"] is False
    assert wire["data"]["deny_reasons"] == ["Your organization's policy check is unavailable."]


async def test_shadow_mode_answers_allow_and_keeps_the_real_verdict() -> None:
    slashid = SlashID(BLOCKED)
    cfg = config(shadow_mode=True)
    decision = await decide(EVENT, token="tok-1", config=cfg, client=slashid.client)
    assert decision.answered.to_wire() == {"verdict": True}
    assert decision.composed.deny_reasons == (BLOCKED,)


def test_a_request_that_cannot_be_judged_takes_the_fail_mode() -> None:
    assert unjudged(config(), "nobody named").answered.to_wire() == {"verdict": True}
    denied = unjudged(config(verdict_fail_mode="deny"), "nobody named").answered
    assert denied.allowed is False and denied.source == "fail_mode"
