"""Composition: any deny denies; a check that cannot answer takes the fail
mode; config-test and unknown types bypass; shadow mode always allows but
keeps the composed verdict for the record."""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
from typing import Any

import httpx
from pydantic import BaseModel
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.checks import Decision
from slashid_anthropic_forwarder.hook.frame import PromptFrame
from slashid_anthropic_forwarder.hook.verdict import RECOVERY, decide, reference_id
from tests.conftest import SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
FILES = [AIAccessedFile(name="a.txt", content_hashes={"sha256": "x"})]
HEADERS = {"webhook-id": "msg_1", "webhook-timestamp": "1", "webhook-signature": "v1,a"}


class Mock(BaseModel):
    """How a check's endpoint answers: a status + JSON body, a transport
    error, or a sleep long enough to blow the budget."""

    status: int = 200
    body: dict[str, Any] | None = None
    error: bool = False
    sleep: float = 0.0


class Expected(BaseModel):
    """``action``/``source`` are the answered verdict — what goes back to
    Anthropic. ``composed_*``, when given, is what the checks decided."""

    action: str
    source: str
    composed_action: str | None = None
    composed_source: str | None = None
    calls: list[str] | None = None  # sorted; None = don't care


def config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.example",
        "push_token": "t",
        "hook_signing_secret": SECRET,
        "gcp_project_id": "proj",
        "shadow_mode": False,
        "preflight_enabled": True,
    }
    base.update(overrides)
    return Config(**base)


def frame(update: dict[str, Any]) -> PromptFrame:
    raw = json.loads((FIXTURES / "frame_tool_result.json").read_text())
    return PromptFrame.model_validate({**raw, **update})


def tail(kind: str) -> AIInvocationObservedV1 | None:
    """What Chunk 6's ``pending._unanswered_round`` hands the composer: the
    partial record for the fresh round, or ``None`` when there is none to
    build — a null ``actor.id`` is dropped there, not here."""
    if kind == "none":
        return None
    return AIInvocationObservedV1(
        request_id="msg_1",
        timestamp="2026-09-20T23:08:20+00:00",
        identity_details=AnthropicIdentityDetails(user_id="user_01A"),
        model=AIModel(id="claude-opus-5", provider="anthropic"),
        parsed_as="anthropic-inference-hook",
        accessed_files=FILES if kind == "with_files" else None,
    )


def router(preflight: Mock | None):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append("preflight")
        spec = preflight
        assert spec is not None, "preflight was called but the case gave it no answer"
        if spec.sleep:
            await asyncio.sleep(spec.sleep)
        if spec.error:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(spec.status, json=spec.body)

    return handler, calls


async def run(cfg: Config, handler, *, tail_kind: str, body: bytes, fr: PromptFrame) -> Decision:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await decide(
            fr,
            raw_body=body,
            headers=HEADERS,
            tail_event=tail(tail_kind),
            config=cfg,
            client=c,
        )


@yaml_pytest(filename="test_decide.yaml")
async def test_decide(
    preflight: Mock | None,
    config_overrides: dict[str, Any],
    frame_update: dict[str, Any],
    tail_kind: str,
    body: str,
    expected: Expected,
) -> None:
    handler, calls = router(preflight)
    decision = await run(
        config(**config_overrides),
        handler,
        tail_kind=tail_kind,
        body=body.encode(),
        fr=frame(frame_update),
    )
    answered = decision.answered
    assert (answered.action, answered.source) == (expected.action, expected.source)
    assert decision.blocked == answered.denied
    if expected.composed_action is not None:
        assert decision.composed.action == expected.composed_action
        assert decision.composed.source == expected.composed_source
    if answered.denied:
        assert answered.reference_id == reference_id("msg_1")
    if expected.calls is not None:
        assert sorted(calls) == expected.calls


async def test_a_long_reason_is_truncated_before_the_sentence_is_appended() -> None:
    """The sentence is the only thing the person can act on, so it survives
    a reason long enough to fill the field on its own."""
    handler, _ = router(Mock(body={"deny_reasons": ["x" * 900]}))
    decision = await run(config(), handler, tail_kind="with_files", body=b"{}", fr=frame({}))
    answered = decision.answered
    reason = answered.deny_reason
    assert reason is not None
    assert len(reason) == 500
    assert reason == "x" * (500 - len(RECOVERY)) + RECOVERY
    # Nothing left for ``to_wire``'s own cap to cut.
    assert answered.to_wire()["deny_reason"] == reason


def test_reference_id_matches_the_go_recipe() -> None:
    assert reference_id("msg_1") == hashlib.sha256(b"msg_1").hexdigest()[:32]
