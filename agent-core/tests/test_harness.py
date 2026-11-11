import base64
import json
import logging
from collections.abc import Callable

import pytest
from pydantic import JsonValue
from slashid_ai_forwarder_core.events import AWSIdentityDetails

from helpers import (
    HARNESS_ROLE,
    LOGGER,
    FakeRoles,
    checker_of,
    events,
    identity_of,
    make_runtime,
)
from slashid_agentcore_forwarder.checks import POLICY_UNAVAILABLE, UNREADABLE, Outcome
from slashid_agentcore_forwarder.deny import derive_key, is_own_deny, with_reference
from slashid_agentcore_forwarder.harness import cache_key, handle
from slashid_agentcore_forwarder.tool_call import CachedInput

ROLE = HARNESS_ROLE
ALLOW = {"decision": "allow", "reason": "slashid"}
Load = Callable[[str], dict[str, JsonValue]]


def drive_before(load: Load, **context: JsonValue) -> dict[str, JsonValue]:
    payload = load("harness_before_tool_call")
    ctx = payload["context"]
    assert isinstance(ctx, dict)
    ctx.update(context)
    return payload


def after_with(
    load: Load, name: str, result: JsonValue, tool_type: str = "agentcore_gateway"
) -> dict[str, JsonValue]:
    payload = load("harness_after_tool_call")
    ctx = payload["context"]
    assert isinstance(ctx, dict)
    ctx.update(toolName=name, toolType=tool_type, toolResult=result)
    return payload


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


READ = "gdrive___read_file_content"
SESSION = "00000000-0000-4000-8000-000000000001"
FIXTURE_HARNESS = (
    "arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/AgentDemo_default_hdemo-EXAMPLE0001"
)
CACHE_KEY = cache_key(FIXTURE_HARNESS, SESSION, "tooluse_EXAMPLE0001")

# before_tool_call


async def test_before_sends_one_call_check_and_allows(load: Load) -> None:
    runtime = make_runtime()
    answer = await handle(runtime, load("harness_before_tool_call"))
    assert answer == ALLOW
    [event] = checker_of(runtime).invocations
    assert event.request_id == f"{SESSION}:00000000-0000-4000-8000-000000000001"
    assert event.conversation_id == SESSION
    assert event.parsed_as == "agentcore-harness-hook"
    assert event.user_agent is None
    assert identity_of(event).principal_arn == ROLE
    [agent] = event.available_agents or []
    assert event.used_agent_ids == [agent.id]
    assert agent.id.endswith("harness/AgentDemo_default_hdemo-EXAMPLE0001")
    assert (agent.provider, agent.name, agent.version) == ("aws-agentcore", "hdemo", "3")
    assert agent.raw_agent_id == "AgentDemo_default_hdemo-EXAMPLE0001"
    assert event.used_tools is None
    [use] = event.requested_tool_uses or []
    assert use.tool_use_id == "tooluse_EXAMPLE0001"
    assert use.is_error is None
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]
    [server] = event.available_tool_servers or []
    assert (server.name, server.kind) == ("gdrive", "mcp")


async def test_the_role_lookup_comes_before_the_budget_starts(load: Load) -> None:
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    await handle(runtime, load("harness_before_tool_call"))
    [looked_up_at] = roles.looked_up_at
    [started] = checker_of(runtime).started
    assert started >= looked_up_at


async def test_a_failed_role_lookup_leaves_the_principal_out(load: Load) -> None:
    roles = FakeRoles(None)
    roles.details = None  # a failed lookup leaves no details either
    runtime = make_runtime(roles=roles)
    await handle(runtime, load("harness_before_tool_call"))
    [event] = checker_of(runtime).invocations
    assert identity_of(event) == AWSIdentityDetails()
    [agent] = event.available_agents or []
    assert (agent.name, agent.version) == (None, None)


async def test_a_remote_mcp_tool_goes_on_the_placeholder_server(load: Load) -> None:
    runtime = make_runtime()
    assert await handle(runtime, load("harness_remote_mcp")) == ALLOW
    [event] = checker_of(runtime).invocations
    [server] = event.available_tool_servers or []
    assert (server.name, server.kind) == ("MCP Tool Server", "mcp")


async def test_a_deny_carries_the_reasons_and_a_verifying_reference(load: Load) -> None:
    runtime = make_runtime(Outcome("deny", ("tagged file",)))
    answer = await handle(runtime, load("harness_before_tool_call"))
    assert isinstance(answer, dict)
    assert answer["decision"] == "deny"
    reason = answer["reason"]
    assert isinstance(reason, str)
    assert "tagged file" in reason
    assert is_own_deny(reason, runtime.key)


async def test_shadow_mode_allows_a_deny_and_logs_it(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime(Outcome("deny", ("no",)), shadow_mode=True)
    assert await handle(runtime, load("harness_before_tool_call")) == ALLOW
    [line] = events(caplog, "shadow")
    assert (line["surface"], line["check"], line["verdict"]) == ("harness", "call", "deny")


@pytest.mark.parametrize(
    ("mode", "allowed"), [("allow", True), ("deny", False)], ids=["fail-open", "fail-closed"]
)
async def test_no_answer_follows_the_fail_mode(
    load: Load, caplog: pytest.LogCaptureFixture, mode: str, allowed: bool
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime(Outcome("no_answer", why="timeout"), verdict_fail_mode=mode)
    answer = await handle(runtime, load("harness_before_tool_call"))
    if allowed:
        assert answer == ALLOW
    else:
        assert isinstance(answer, dict)
        assert answer["decision"] == "deny"
        reason = answer["reason"]
        assert isinstance(reason, str)
        assert reason.startswith(POLICY_UNAVAILABLE)
        assert is_own_deny(reason, runtime.key)
    [line] = events(caplog, "fail_mode")
    assert (line["check"], line["reason"], line["mode"]) == ("call", "timeout", mode)


@pytest.mark.parametrize("field", ["toolName", "toolUseId"])
@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_an_unreadable_envelope_is_denied_whatever_the_fail_mode(
    load: Load, caplog: pytest.LogCaptureFixture, field: str, fail_mode: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("harness_before_tool_call")
    ctx = payload["context"]
    assert isinstance(ctx, dict)
    del ctx[field]
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await handle(runtime, payload)
    assert isinstance(answer, dict)
    assert answer["decision"] == "deny"
    reason = answer["reason"]
    assert isinstance(reason, str)
    assert reason.startswith(UNREADABLE)
    assert checker_of(runtime).invocations == []
    [line] = events(caplog, "unreadable")
    assert (line["surface"], line["check"], line["reason"]) == ("harness", "call", "bad_envelope")


async def test_an_unreadable_envelope_is_allowed_in_shadow_mode(load: Load) -> None:
    payload = load("harness_before_tool_call")
    ctx = payload["context"]
    assert isinstance(ctx, dict)
    del ctx["toolName"]
    runtime = make_runtime(shadow_mode=True)
    assert await handle(runtime, payload) == ALLOW
    assert checker_of(runtime).invocations == []


async def test_a_truncated_input_is_logged_and_gives_no_refs(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = drive_before(load, toolInput={"truncated": True, "preview": '{"fileId": "ID1'})
    runtime = make_runtime()
    assert await handle(runtime, payload) == ALLOW
    [event] = checker_of(runtime).invocations
    assert event.accessed_files is None
    [line] = events(caplog, "truncated_input")
    assert line["tool"] == READ
    assert line["call_id"] == "tooluse_EXAMPLE0001"
    assert "ID1" not in json.dumps(line)
    assert runtime.inputs.get(CACHE_KEY) is None


# the input cache


async def test_an_allowed_call_with_a_file_ref_is_cached(load: Load) -> None:
    runtime = make_runtime()
    await handle(runtime, load("harness_before_tool_call"))
    assert runtime.inputs.get(CACHE_KEY) == CachedInput(
        tool_name=READ, tool_input={"fileId": "ID1"}
    )


async def test_a_call_without_file_refs_is_not_cached(load: Load) -> None:
    runtime = make_runtime()
    payload = drive_before(
        load,
        toolName="shell",
        toolType="builtin",
        toolInput={"command": "echo secret"},
    )
    await handle(runtime, payload)
    assert runtime.inputs.get(CACHE_KEY) is None


async def test_a_denied_call_is_not_cached(load: Load) -> None:
    runtime = make_runtime(Outcome("deny", ("no",)))
    await handle(runtime, load("harness_before_tool_call"))
    assert runtime.inputs.get(CACHE_KEY) is None


async def test_a_shadow_denied_call_is_cached(load: Load) -> None:
    runtime = make_runtime(Outcome("deny", ("no",)), shadow_mode=True)
    await handle(runtime, load("harness_before_tool_call"))
    assert runtime.inputs.get(CACHE_KEY) is not None


async def test_a_call_allowed_by_the_fail_mode_is_cached(load: Load) -> None:
    runtime = make_runtime(Outcome("no_answer", why="timeout"))
    await handle(runtime, load("harness_before_tool_call"))
    assert runtime.inputs.get(CACHE_KEY) is not None


@pytest.mark.parametrize(
    "outcome", [Outcome("allow"), Outcome("deny", ("no",))], ids=["allowed", "denied"]
)
async def test_a_call_that_is_not_cached_drops_the_stale_entry_of_its_key(
    load: Load, caplog: pytest.LogCaptureFixture, outcome: Outcome
) -> None:
    """A repeated ``call_0`` must not hand the earlier call's input to this call's result."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime(outcome)
    runtime.inputs[CACHE_KEY] = CachedInput(tool_name=READ, tool_input={"fileId": "STALE"})
    await handle(runtime, drive_before(load, toolInput={"query": "no file ref here"}))
    assert runtime.inputs.get(CACHE_KEY) is None
    await handle(runtime, after_with(load, READ, drive_result("NEW")))
    [line] = events(caplog, "cache_miss")
    assert line["tool"] == READ
    [event] = checker_of(runtime).invocations[-1:]
    assert [f.file_refs for f in event.accessed_files or []] == [["google/NEW"]]


async def test_a_repeated_call_id_across_turns_does_not_collide(load: Load) -> None:
    runtime = make_runtime()
    for turn, file_id in enumerate(["A", "B"]):
        payload = drive_before(load, toolUseId="call_0", toolInput={"fileId": file_id})
        payload["hookEventId"] = f"event-{turn}"
        await handle(runtime, payload)
        text = json.dumps({"content": b64(file_id.encode()), "id": file_id, "title": "t.md"})
        after = after_with(load, READ, {"status": "success", "content": [{"text": text}]})
        ctx = after["context"]
        assert isinstance(ctx, dict)
        ctx["toolUseId"] = "call_0"
        after["hookEventId"] = f"after-{turn}"
        await handle(runtime, after)
    sent = [e for e in checker_of(runtime).invocations if e.used_tools]
    assert [[f.file_refs for f in e.accessed_files or []] for e in sent] == [
        [["google/A"]],
        [["google/B"]],
    ]


# after_tool_call


def drive_result(file_id: str = "ID1") -> JsonValue:
    text = json.dumps({"content": b64(b"# Bread\n"), "id": file_id, "title": "banana-bread.md"})
    return {"status": "success", "content": [{"text": text}]}


async def test_after_sends_a_result_check_with_the_cached_input(load: Load) -> None:
    runtime = make_runtime()
    payload = after_with(load, "gdrive___download_file_content", drive_result())
    runtime.inputs[CACHE_KEY] = CachedInput(
        tool_name="gdrive___download_file_content", tool_input={"fileId": "ID1"}
    )
    assert await handle(runtime, payload) == ALLOW
    [event] = checker_of(runtime).invocations
    assert event.requested_tool_uses is None
    assert event.request_id == f"{SESSION}:00000000-0000-4000-8000-000000000002"
    [use] = event.used_tools or []
    assert (use.tool_use_id, use.is_error) == ("tooluse_EXAMPLE0001", False)
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]
    assert file.name == "banana-bread.md"
    assert file.content_hashes is not None
    assert runtime.inputs.get(CACHE_KEY) is None


async def test_after_pops_the_entry_even_for_our_own_deny(load: Load) -> None:
    runtime = make_runtime()
    runtime.inputs[CACHE_KEY] = CachedInput(tool_name=READ, tool_input={"fileId": "ID1"})
    text = with_reference("Blocked by policy.", runtime.key)
    payload = after_with(load, READ, {"status": "error", "content": [{"text": text}]})
    assert await handle(runtime, payload) == ALLOW
    assert runtime.inputs.get(CACHE_KEY) is None
    assert checker_of(runtime).invocations == []


@pytest.mark.parametrize("status", ["success", "error"])
async def test_our_own_deny_is_allowed_without_a_request(
    load: Load, caplog: pytest.LogCaptureFixture, status: str
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    roles = FakeRoles()
    runtime = make_runtime(roles=roles)
    text = with_reference("Content withheld: no", runtime.key)
    payload = after_with(
        load, READ, {"status": status, "content": [{"type": "text", "text": text}]}
    )
    assert await handle(runtime, payload) == ALLOW
    assert checker_of(runtime).invocations == []
    assert roles.looked_up == []
    assert events(caplog, "cache_miss") == []


async def test_a_deny_text_with_a_second_block_is_checked(load: Load) -> None:
    runtime = make_runtime()
    text = with_reference("Blocked", runtime.key)
    result: JsonValue = {"status": "error", "content": [{"text": text}, {"text": "more"}]}
    await handle(runtime, after_with(load, READ, result))
    assert len(checker_of(runtime).invocations) == 1


async def test_a_deny_text_with_structured_content_is_checked(load: Load) -> None:
    runtime = make_runtime()
    text = with_reference("Blocked", runtime.key)
    result: JsonValue = {
        "status": "error",
        "content": [{"text": text}],
        "structuredContent": {"a": 1},
    }
    await handle(runtime, after_with(load, READ, result))
    assert len(checker_of(runtime).invocations) == 1


async def test_a_deny_text_signed_with_another_key_is_checked(load: Load) -> None:
    runtime = make_runtime()
    text = with_reference("Blocked", derive_key("other-token"))
    result: JsonValue = {"status": "error", "content": [{"text": text}]}
    await handle(runtime, after_with(load, READ, result))
    assert len(checker_of(runtime).invocations) == 1


async def test_a_cached_entry_for_another_tool_is_a_miss(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime()
    runtime.inputs[CACHE_KEY] = CachedInput(
        tool_name="gdrive___search_files", tool_input={"fileId": "ID1"}
    )
    await handle(runtime, after_with(load, READ, {"status": "success", "content": [{"text": "x"}]}))
    [event] = checker_of(runtime).invocations
    assert event.accessed_files is None
    [line] = events(caplog, "cache_miss")
    assert line["tool"] == READ
    assert runtime.inputs.get(CACHE_KEY) is None


async def test_a_miss_is_logged_at_info_for_a_connector_tool(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    runtime = make_runtime()
    await handle(runtime, after_with(load, READ, drive_result()))
    [line] = events(caplog, "cache_miss")
    assert (line["tool"], line["call_id"], line["known"]) == (
        READ,
        "tooluse_EXAMPLE0001",
        True,
    )
    [record] = [r for r in caplog.records if '"cache_miss"' in r.getMessage()]
    assert record.levelno == logging.INFO
    [event] = checker_of(runtime).invocations
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]  # the result names the file


async def test_a_miss_is_debug_for_another_tool(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    runtime = make_runtime()
    await handle(runtime, load("harness_after_inline"))
    [record] = [r for r in caplog.records if '"cache_miss"' in r.getMessage()]
    assert record.levelno == logging.DEBUG
    assert json.loads(record.getMessage())["known"] is False


async def test_an_error_result_is_checked_without_files(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    runtime = make_runtime()
    runtime.inputs[CACHE_KEY] = CachedInput(tool_name=READ, tool_input={"fileId": "ID1"})
    result: JsonValue = {"status": "error", "content": [{"text": "boom"}]}
    assert await handle(runtime, after_with(load, READ, result)) == ALLOW
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert use.is_error is True
    assert event.accessed_files is None
    assert events(caplog, "cache_miss") == []


async def test_is_error_flag_marks_an_error(load: Load) -> None:
    runtime = make_runtime()
    result: JsonValue = {"isError": True, "content": [{"text": "boom"}]}
    await handle(runtime, after_with(load, "shell", result, "builtin"))
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert use.is_error is True


async def test_a_truncated_result_takes_refs_from_the_cached_input(load: Load) -> None:
    runtime = make_runtime()
    runtime.inputs[CACHE_KEY] = CachedInput(tool_name=READ, tool_input={"fileId": "ID1"})
    preview = json.dumps({"content": b64(b"x"), "id": "PREVIEW", "title": "p.md"})
    payload = after_with(load, READ, {"truncated": True, "preview": preview})
    assert await handle(runtime, payload) == ALLOW
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert use.is_error is False
    [file] = event.accessed_files or []
    assert (file.file_refs, file.content_hashes, file.name) == (["google/ID1"], None, None)


async def test_a_truncated_result_without_input_gives_no_files(load: Load) -> None:
    runtime = make_runtime()
    await handle(runtime, load("harness_after_truncated"))
    [event] = checker_of(runtime).invocations
    assert event.accessed_files is None
    [use] = event.used_tools or []
    assert use.is_error is False


async def test_a_non_object_result_is_a_result_with_no_references(load: Load) -> None:
    runtime = make_runtime()
    runtime.inputs[CACHE_KEY] = CachedInput(tool_name=READ, tool_input={"fileId": "ID1"})
    assert await handle(runtime, after_with(load, READ, "just a string")) == ALLOW
    [event] = checker_of(runtime).invocations
    [use] = event.used_tools or []
    assert use.is_error is False
    [file] = event.accessed_files or []
    assert file.file_refs == ["google/ID1"]  # from the cached input alone


async def test_a_result_with_structured_content_is_read(load: Load) -> None:
    runtime = make_runtime()
    runtime.inputs[CACHE_KEY] = CachedInput(
        tool_name="gdrive___search_files", tool_input={"query": "q"}
    )
    hits: JsonValue = {"files": [{"id": "A", "title": "a.md", "mimeType": "text/markdown"}]}
    result: JsonValue = {"status": "success", "content": [], "structuredContent": hits}
    await handle(runtime, after_with(load, "gdrive___search_files", result))
    [event] = checker_of(runtime).invocations
    [file] = event.accessed_files or []
    assert (file.file_refs, file.name) == (["google/A"], "a.md")


async def test_after_deny_has_no_withheld_prefix(load: Load) -> None:
    runtime = make_runtime(Outcome("deny", ("no",)))
    answer = await handle(runtime, load("harness_after_inline"))
    assert isinstance(answer, dict)
    assert answer["decision"] == "deny"
    reason = answer["reason"]
    assert isinstance(reason, str)
    assert reason.startswith("no")
    assert is_own_deny(reason, runtime.key)


async def test_after_shadow_allows_a_deny(load: Load, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime(Outcome("deny", ("no",)), shadow_mode=True)
    assert await handle(runtime, load("harness_after_inline")) == ALLOW
    [line] = events(caplog, "shadow")
    assert line["check"] == "result"


@pytest.mark.parametrize(("mode", "decision"), [("allow", "allow"), ("deny", "deny")])
async def test_after_no_answer_follows_the_fail_mode(load: Load, mode: str, decision: str) -> None:
    runtime = make_runtime(Outcome("no_answer", why="status"), verdict_fail_mode=mode)
    answer = await handle(runtime, load("harness_after_inline"))
    assert isinstance(answer, dict)
    assert answer["decision"] == decision


@pytest.mark.parametrize(("mode", "decision"), [("allow", "allow"), ("deny", "deny")])
async def test_an_invalid_after_envelope_takes_the_no_answer_path(
    load: Load, caplog: pytest.LogCaptureFixture, mode: str, decision: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("harness_after_inline")
    ctx = payload["context"]
    assert isinstance(ctx, dict)
    del ctx["toolUseId"]
    runtime = make_runtime(verdict_fail_mode=mode)
    answer = await handle(runtime, payload)
    assert isinstance(answer, dict)
    assert answer["decision"] == decision
    assert checker_of(runtime).invocations == []
    [line] = events(caplog, "fail_mode")
    assert (line["check"], line["reason"]) == ("result", "error")
    assert events(caplog, "unreadable") == []


async def test_no_log_line_carries_payload_text(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    runtime = make_runtime(Outcome("deny", ("no",)), shadow_mode=True)
    await handle(runtime, load("harness_before_tool_call"))
    await handle(runtime, after_with(load, READ, drive_result("SECRET-ID")))
    assert "SECRET-ID" not in caplog.text
    assert "banana-bread" not in caplog.text


# the raw payload, whitespace and all


async def test_the_cached_input_and_its_key_are_not_stripped(load: Load) -> None:
    payload = drive_before(load, toolInput={"fileId": "ID1 ", "note": "line\n"})
    payload["sessionId"] = " abc "
    runtime = make_runtime()
    await handle(runtime, payload)
    cached = runtime.inputs.get(cache_key(FIXTURE_HARNESS, " abc ", "tooluse_EXAMPLE0001"))
    assert cached is not None
    assert cached.tool_input == {"fileId": "ID1 ", "note": "line\n"}
    assert runtime.inputs.get(cache_key(FIXTURE_HARNESS, "abc", "tooluse_EXAMPLE0001")) is None


@pytest.mark.parametrize("field", ["toolName", "toolUseId"])
async def test_a_blank_name_or_id_is_still_unreadable(load: Load, field: str) -> None:
    payload = drive_before(load, **{field: " \t\n"})
    runtime = make_runtime(verdict_fail_mode="allow")
    answer = await handle(runtime, payload)
    assert isinstance(answer, dict)
    assert answer["decision"] == "deny"
    assert checker_of(runtime).invocations == []


# the event name

BAD_EVENTS: list[JsonValue] = [
    None,
    7,
    ["before_tool_call"],
    {},
    True,
    "weird",
    "",
    "BEFORE_TOOL_CALL",
]


@pytest.mark.parametrize("event", BAD_EVENTS)
@pytest.mark.parametrize("fail_mode", ["allow", "deny"])
async def test_an_event_that_is_not_a_tool_hook_takes_the_no_answer_path(
    load: Load, caplog: pytest.LogCaptureFixture, event: JsonValue, fail_mode: str
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    payload = load("harness_before_tool_call")
    payload["event"] = event
    runtime = make_runtime(verdict_fail_mode=fail_mode)
    answer = await handle(runtime, payload)
    assert isinstance(answer, dict)
    assert answer["decision"] == fail_mode
    assert checker_of(runtime).invocations == []
    [line] = events(caplog, "fail_mode")
    assert (line["check"], line["reason"]) == ("call", "unknown_event")
    assert events(caplog, "unreadable") == []


async def test_a_payload_without_an_event_takes_the_no_answer_path(load: Load) -> None:
    payload = load("harness_before_tool_call")
    del payload["event"]
    runtime = make_runtime(verdict_fail_mode="deny")
    answer = await handle(runtime, payload)
    assert isinstance(answer, dict)
    assert answer["decision"] == "deny"


# the input cache key


async def test_the_same_ids_on_two_harnesses_do_not_share_an_entry(load: Load) -> None:
    runtime = make_runtime()
    arns = {
        "A": "arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/harness-A",
        "B": "arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/harness-B",
    }
    for file_id, arn in arns.items():
        payload = drive_before(load, toolInput={"fileId": file_id})
        payload["harnessArn"] = arn
        await handle(runtime, payload)
    for file_id, arn in arns.items():
        after = after_with(load, READ, {"truncated": True, "preview": "{"})
        after["harnessArn"] = arn
        await handle(runtime, after)
        event = checker_of(runtime).invocations[-1]
        assert [f.file_refs for f in event.accessed_files or []] == [[f"google/{file_id}"]]


async def test_ids_that_join_to_the_same_text_do_not_share_an_entry(load: Load) -> None:
    runtime = make_runtime()
    pairs = [("a:b", "c", "X"), ("a", "b:c", "Y")]
    for session, tool_use, file_id in pairs:
        payload = drive_before(load, toolUseId=tool_use, toolInput={"fileId": file_id})
        payload["sessionId"] = session
        await handle(runtime, payload)
    for session, tool_use, file_id in pairs:
        after = after_with(load, READ, {"truncated": True, "preview": "{"})
        after["sessionId"] = session
        context = after["context"]
        assert isinstance(context, dict)
        context["toolUseId"] = tool_use
        await handle(runtime, after)
        event = checker_of(runtime).invocations[-1]
        assert [f.file_refs for f in event.accessed_files or []] == [[f"google/{file_id}"]]
