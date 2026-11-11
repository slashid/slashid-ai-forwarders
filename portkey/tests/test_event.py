"""A Portkey webhook body becomes the event SlashID judges and the one it records."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from slashid_ai_forwarder_core.events import AIGatewayIdentityDetails

from slashid_portkey_forwarder.config import Config
from slashid_portkey_forwarder.event import after_events, before_event, identity_of
from slashid_portkey_forwarder.hook import Hook, parse_hook
from tests.hooks import FILE_BYTES, REQUEST, TEXT_ANSWER, after, before, completion, tool_call

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
ADA = AIGatewayIdentityDetails(user_email="ada@example.com")


def config(**overrides: Any) -> Config:
    settings: dict[str, Any] = {"endpoint": "https://api.slashid.example", **overrides}
    return Config(**settings)


def hook(body: dict[str, Any]) -> Hook:
    parsed = parse_hook(body)
    assert parsed is not None
    return parsed


def test_the_user_metadata_key_names_a_person_by_email() -> None:
    assert identity_of({"_user": " Ada@Example.com "}, config()) == ADA


def test_a_user_that_is_not_an_address_is_a_user_id() -> None:
    assert identity_of({"_user": "u-42"}, config()) == AIGatewayIdentityDetails(user_id="u-42")


def test_the_agent_metadata_key_names_an_agent_beside_the_user() -> None:
    identity = identity_of({"_user": "ada@example.com", "agent": "stylist"}, config())
    assert identity == AIGatewayIdentityDetails(user_email="ada@example.com", agent_id="stylist")


def test_the_metadata_keys_are_configurable() -> None:
    cfg = config(user_metadata_key="email", agent_metadata_key="app")
    assert identity_of({"email": "ada@example.com", "app": "stitchy"}, cfg) == (
        AIGatewayIdentityDetails(user_email="ada@example.com", agent_id="stitchy")
    )


def test_metadata_that_names_nobody_gives_no_identity() -> None:
    assert identity_of({"team": "styling", "_user": "  "}, config()) is None


async def test_before_event_carries_who_asked_which_model_and_the_fresh_round() -> None:
    event = await before_event(hook(before()), ADA, config=config(), now=NOW)
    assert event is not None
    assert event.identity_details == ADA
    assert (event.model.id, event.model.provider, event.model.raw_model_id) == (
        "gpt-5",
        "openai",
        "@openai-prod/gpt-5",
    )
    assert event.parsed_as == "portkey-openai-chat"
    assert event.timestamp == "2026-11-10T12:00:00+00:00"
    assert event.requested_tool_uses is None and event.output is None
    assert [t.name for t in event.available_tools or []] == ["create_issue"]
    assert [s.name for s in event.available_tool_servers or []] == ["github"]


async def test_before_event_sends_the_prompt_text_even_when_records_keep_none() -> None:
    event = await before_event(hook(before()), ADA, config=config(), now=NOW)
    assert event is not None and event.input is not None
    assert "summarize project falcon" in (event.input.redacted_text or "")


async def test_before_event_keeps_the_prompt_text_back_when_told_to() -> None:
    cfg = config(preflight_content=False)
    event = await before_event(hook(before()), ADA, config=cfg, now=NOW)
    assert event is not None and event.input is not None
    assert event.input.redacted_text is None
    assert event.input.content_hashes


TOOL_ROUND: dict[str, Any] = {
    **REQUEST,
    "messages": [
        {"role": "user", "content": "look up falcon"},
        {"role": "assistant", "content": None, "tool_calls": [tool_call("call_1", "x")]},
        {"role": "tool", "tool_call_id": "call_1", "content": "falcon launch is 2027-01-15"},
        {"role": "user", "content": "and who owns it?"},
    ],
}


async def test_before_event_sends_what_a_tool_returned_as_that_tools_result() -> None:
    event = await before_event(hook(before(TOOL_ROUND)), ADA, config=config(), now=NOW)
    assert event is not None
    [used] = event.used_tools or []
    assert used.tool_use_id == "call_1"
    assert used.result == "falcon launch is 2027-01-15"
    assert [t.name for t in event.available_tools or [] if t.id == used.tool_id] == ["create_issue"]


async def test_before_event_keeps_a_tool_result_out_of_the_prompt_text() -> None:
    """A rule on prompts reads what was asked, a rule on tool results what
    came back: one text holding both would let either rule block the other's."""
    event = await before_event(hook(before(TOOL_ROUND)), ADA, config=config(), now=NOW)
    assert event is not None and event.input is not None
    text = event.input.redacted_text or ""
    assert "and who owns it?" in text
    assert "falcon launch is 2027-01-15" not in text
    # The round before the last answer was judged when it was sent.
    assert "look up falcon" not in text


async def test_before_event_of_a_round_of_tool_results_alone_has_an_empty_prompt() -> None:
    request = {**TOOL_ROUND, "messages": TOOL_ROUND["messages"][:3]}
    event = await before_event(hook(before(request)), ADA, config=config(), now=NOW)
    assert event is not None and event.input is not None
    # Sent, and empty: no rule on prompts matches it, and none finds it missing.
    assert event.input.redacted_text == ""
    assert [u.result for u in event.used_tools or []] == ["falcon launch is 2027-01-15"]


async def test_before_event_keeps_a_tool_result_back_with_the_prompt_text() -> None:
    cfg = config(preflight_content=False)
    event = await before_event(hook(before(TOOL_ROUND)), ADA, config=cfg, now=NOW)
    assert event is not None
    assert [u.result for u in event.used_tools or []] == [None]


async def test_before_event_cuts_a_long_tool_result_like_any_other_text() -> None:
    long = "a" * 400 + "MIDDLE" + "z" * 400
    request = {
        **TOOL_ROUND,
        "messages": [*TOOL_ROUND["messages"][:2], {**TOOL_ROUND["messages"][2], "content": long}],
    }
    event = await before_event(
        hook(before(request)), ADA, config=config(max_content_size=200), now=NOW
    )
    assert event is not None
    [used] = event.used_tools or []
    assert used.result is not None and len(used.result) <= 200
    assert (
        used.result.startswith("aaa")
        and used.result.endswith("zzz")
        and "MIDDLE" not in used.result
    )


async def test_before_event_hashes_an_inline_file() -> None:
    event = await before_event(hook(before()), ADA, config=config(), now=NOW)
    assert event is not None
    [file] = event.accessed_files or []
    assert (file.name, file.byte_length, file.provenance) == (
        "q3.txt",
        len(FILE_BYTES),
        "attachment",
    )
    assert (file.content_hashes or {})["sha256"] == hashlib.sha256(FILE_BYTES).hexdigest()
    assert file.redacted_content is None


async def test_a_request_that_is_not_a_chat_gives_no_event() -> None:
    assert (
        await before_event(hook(before({"input": "embed me"})), ADA, config=config(), now=NOW)
        is None
    )
    not_chat = hook(before(request_type="embed"))
    assert await before_event(not_chat, ADA, config=config(), now=NOW) is None


async def test_the_same_request_gets_the_same_id_and_another_user_another() -> None:
    first = await before_event(hook(before()), ADA, config=config(), now=NOW)
    again = await before_event(hook(before()), ADA, config=config(), now=NOW)
    other = await before_event(
        hook(before()), AIGatewayIdentityDetails(user_id="u-42"), config=config(), now=NOW
    )
    assert first is not None and again is not None and other is not None
    assert first.request_id == again.request_id
    assert first.request_id.startswith("pk_")
    assert other.request_id != first.request_id


async def test_after_record_is_the_completed_invocation() -> None:
    result = await after_events(hook(after(completion(TEXT_ANSWER))), ADA, config=config(), now=NOW)
    assert result is not None
    record = result.record
    assert (record.tokens.input, record.tokens.output) == (12, 5)
    assert record.stop_reason == "end_turn"
    assert record.output is not None and record.output.redacted_text is None
    assert record.input is not None and record.input.redacted_text is None
    assert [f.name for f in record.accessed_files or []] == ["q3.txt"]


async def test_after_record_id_extends_the_before_id_and_differs_per_response() -> None:
    sent = await before_event(hook(before()), ADA, config=config(), now=NOW)
    one = await after_events(hook(after(completion(TEXT_ANSWER))), ADA, config=config(), now=NOW)
    two = await after_events(
        hook(after(completion(TEXT_ANSWER, response_id="chatcmpl-2"))),
        ADA,
        config=config(),
        now=NOW,
    )
    assert sent is not None and one is not None and two is not None
    assert one.record.request_id.startswith(sent.request_id + "_")
    assert one.record.request_id != two.record.request_id


async def test_after_judges_the_tool_calls_the_model_asked_for_with_their_arguments() -> None:
    answer = {"role": "assistant", "content": None, "tool_calls": [tool_call("call_1", "ship it")]}
    result = await after_events(hook(after(completion(answer))), ADA, config=config(), now=NOW)
    assert result is not None and result.judged is not None
    judged = result.judged
    [use] = judged.requested_tool_uses or []
    assert use.arguments == {"title": "ship it"}
    assert use.tool_use_id == "call_1"
    assert [t.id for t in judged.available_tools or []] == [use.tool_id]
    assert [s.name for s in judged.available_tool_servers or []] == ["github"]
    # The prompt was judged before the request left; only the calls are new.
    assert judged.input is None and judged.accessed_files is None
    assert judged.request_id == result.record.request_id
    assert [u.tool_use_id for u in result.record.requested_tool_uses or []] == ["call_1"]
    assert (result.record.requested_tool_uses or [])[0].arguments is None


async def test_after_judges_the_tool_calls_of_every_choice() -> None:
    first = {"role": "assistant", "content": None, "tool_calls": [tool_call("call_0", "one")]}
    second = {"role": "assistant", "content": None, "tool_calls": [tool_call("call_0", "two")]}
    result = await after_events(
        hook(after(completion(first, second))), ADA, config=config(), now=NOW
    )
    assert result is not None and result.judged is not None
    assert [u.arguments for u in result.judged.requested_tool_uses or []] == [
        {"title": "one"},
        {"title": "two"},
    ]


async def test_after_judges_the_answer_itself_when_the_model_asked_for_no_tool() -> None:
    result = await after_events(hook(after(completion(TEXT_ANSWER))), ADA, config=config(), now=NOW)
    assert result is not None and result.judged is not None
    judged = result.judged
    assert judged.output is not None and judged.output.redacted_text == "Falcon is on track."
    assert judged.requested_tool_uses is None
    # The prompt was judged before the request left.
    assert judged.input is None and judged.used_tools is None
    assert judged.request_id == result.record.request_id
    # The record follows its own setting: no text unless asked for.
    assert result.record.output is not None and result.record.output.redacted_text is None


async def test_after_judges_the_text_of_every_choice() -> None:
    other = {"role": "assistant", "content": "Falcon slipped to March."}
    result = await after_events(
        hook(after(completion(TEXT_ANSWER, other))), ADA, config=config(), now=NOW
    )
    assert result is not None and result.judged is not None and result.judged.output is not None
    assert result.judged.output.redacted_text == "Falcon is on track.\n\nFalcon slipped to March."


async def test_after_judges_the_text_that_comes_with_tool_calls() -> None:
    answer = {
        "role": "assistant",
        "content": "Opening the issue.",
        "tool_calls": [tool_call("call_1", "ship it")],
    }
    result = await after_events(hook(after(completion(answer))), ADA, config=config(), now=NOW)
    assert result is not None and result.judged is not None and result.judged.output is not None
    assert result.judged.output.redacted_text == "Opening the issue."
    assert [u.arguments for u in result.judged.requested_tool_uses or []] == [{"title": "ship it"}]


async def test_after_says_there_was_an_answer_it_keeps_back_when_told_to() -> None:
    """An answer whose text was not sent is one a rule on answers cannot
    read, and denies; leaving the answer out would read as none to judge."""
    cfg = config(preflight_content=False)
    result = await after_events(hook(after(completion(TEXT_ANSWER))), ADA, config=cfg, now=NOW)
    assert result is not None and result.judged is not None and result.judged.output is not None
    assert result.judged.output.redacted_text is None
    assert result.judged.output.content_hashes


async def test_after_with_no_response_body_still_records_the_request() -> None:
    """A streamed answer reaches the webhook with no body."""
    one = await after_events(hook(after({})), ADA, config=config(), now=NOW)
    two = await after_events(hook(after(None)), ADA, config=config(), now=NOW)
    assert one is not None and two is not None
    assert one.judged is None and one.record.output is None
    assert one.record.input is not None
    assert one.record.request_id != two.record.request_id
