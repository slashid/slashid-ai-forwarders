"""OpenAI Responses → NormalizedInvocation."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from pydantic_extra_types.mime_types import MimeType

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import (
    AIInvocationTokens,
    AIModel,
    AIToolUse,
    EventEnvelope,
    OpenAIIdentityDetails,
    build_event_from_normalized,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedContent
from slashid_ai_forwarder_core.normalize.openai.responses.normalize import (
    responses_stream_to_normalized_invocation,
    responses_to_normalized_invocation,
    to_normalized,
)
from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    Response,
    ResponsesRequest,
    ResponseStreamEvent,
)
from slashid_ai_forwarder_core.rounds import START, round_links

_FIXTURES = Path(__file__).parent / "fixtures"
_EVENTS = TypeAdapter(list[ResponseStreamEvent])
_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def _response(*output: dict[str, Any], status: str = "completed") -> Response:
    return Response.model_validate(
        {"object": "response", "id": "resp_1", "status": status, "output": list(output)}
    )


async def test_plain_fixture() -> None:
    record = _load("openai_responses_mil.json")
    normalized = await responses_to_normalized_invocation(
        ResponsesRequest.model_validate(record["input"]["inputBodyJson"]),
        Response.model_validate(record["output"]["outputBodyJson"]),
        config=_CONFIG,
    )
    assert [(m.role, m.content) for m in normalized.input.messages] == [
        ("user", [NormalizedContent(kind="text", text="Say hello in three words.")])
    ]
    assert normalized.output.message is not None
    assert normalized.output.message.role == "assistant"
    assert normalized.output.message.content == [
        NormalizedContent(kind="reasoning", text=None),
        NormalizedContent(kind="text", text="Hello there, friend!"),
    ]
    assert normalized.output.stop_reason == "end_turn"
    assert normalized.tokens == AIInvocationTokens(input=12, output=11, reasoning=12)


async def _stream_fixture() -> Any:
    record = _load("openai_responses_stream_mil.json")
    return await responses_stream_to_normalized_invocation(
        ResponsesRequest.model_validate(record["input"]["inputBodyJson"]),
        _EVENTS.validate_python(record["output"]["outputBodyJson"]),
        config=_CONFIG,
    )


async def test_stream_fixture() -> None:
    normalized = await _stream_fixture()
    assert [(m.role, m.content[0].text) for m in normalized.input.messages] == [
        ("system", "Use the tool."),
        ("user", "What is the weather in Lisbon?"),
    ]
    assert normalized.output.message is not None
    assert normalized.output.message.content == [
        NormalizedContent(
            kind="tool_use",
            tool_use_id="call_cee16a2c0447526b810260c5bad53c78",
            tool_name="get_weather",
            tool_input={"city": "Lisbon"},
            tool_executor="client",
        )
    ]
    assert normalized.output.stop_reason == "tool_use"
    assert normalized.tokens == AIInvocationTokens(input=56, output=19)
    tool = normalized.input.tools_declared[0]
    assert tool.name == "get_weather"
    servers = {s.id: s.name for s in normalized.input.tool_servers}
    assert servers[tool.tool_server_id] == "builtin"


async def test_stream_without_terminal_event_has_no_output() -> None:
    normalized = await responses_stream_to_normalized_invocation(
        ResponsesRequest(input="hi"), [ResponseStreamEvent(type="error")], config=_CONFIG
    )
    assert [m.role for m in normalized.input.messages] == ["user"]
    assert normalized.output.message is None
    assert normalized.output.stop_reason == "unknown"


def _history_request() -> ResponsesRequest:
    return ResponsesRequest.model_validate(
        {
            "instructions": "be helpful",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "sure"}],
                },
                {"type": "reasoning", "summary": [{"text": "a"}, {"text": "b"}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell",
                    "arguments": '{"command": "ls"}',
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
                {
                    "type": "custom_tool_call",
                    "call_id": "call_2",
                    "name": "apply_patch",
                    "input": "*** Begin Patch",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_2",
                    "output": [{"type": "input_text", "text": "done"}],
                },
                {"role": "assistant", "content": "patched"},
                {"type": "compaction", "encrypted_content": "opaque"},
                {"type": "image_generation_call", "id": "ig_1"},
                {"role": "developer", "content": "be brief"},
                {"role": "user", "content": "next"},
            ],
        }
    )


def test_history_items_map_to_blocks_and_roles() -> None:
    normalized = to_normalized(_history_request(), _response())
    digest = hashlib.sha256(b"opaque").hexdigest()
    assert [(m.role, m.content) for m in normalized.input.messages] == [
        ("system", [NormalizedContent(kind="text", text="be helpful")]),
        ("user", [NormalizedContent(kind="text", text="hi")]),
        (
            "assistant",
            [
                NormalizedContent(kind="text", text="sure"),
                NormalizedContent(kind="reasoning", text="a\nb"),
                NormalizedContent(
                    kind="tool_use",
                    tool_use_id="call_1",
                    tool_name="shell",
                    tool_input={"command": "ls"},
                    tool_executor="client",
                ),
            ],
        ),
        (
            "user",
            [
                NormalizedContent(
                    kind="tool_result",
                    tool_use_id="call_1",
                    tool_output="a.txt",
                    tool_executor="client",
                )
            ],
        ),
        (
            "assistant",
            [
                NormalizedContent(
                    kind="tool_use",
                    tool_use_id="call_2",
                    tool_name="apply_patch",
                    tool_input="*** Begin Patch",
                    tool_executor="client",
                )
            ],
        ),
        (
            "user",
            [
                NormalizedContent(
                    kind="tool_result",
                    tool_use_id="call_2",
                    tool_output=[{"type": "input_text", "text": "done"}],
                    tool_executor="client",
                )
            ],
        ),
        ("assistant", [NormalizedContent(kind="text", text="patched")]),
        ("assistant", [NormalizedContent(kind="compaction", text=digest)]),
        ("system", [NormalizedContent(kind="text", text="be brief")]),
        ("user", [NormalizedContent(kind="text", text="next")]),
    ]


def test_compaction_is_never_merged_with_a_following_assistant_item() -> None:
    request = ResponsesRequest.model_validate(
        {
            "input": [
                {"role": "user", "content": "hi"},
                {"type": "compaction", "encrypted_content": "x"},
                {"role": "assistant", "content": "after"},
            ]
        }
    )
    messages = to_normalized(request, _response()).input.messages
    assert [[c.kind for c in m.content] for m in messages] == [["text"], ["compaction"], ["text"]]
    assert messages[1].content[0].text


def test_unparseable_arguments_and_web_search_and_images() -> None:
    png = b"\x89PNG\r\n\x1a\nxyz"
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    request = ResponsesRequest.model_validate(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": data_url},
                        {"type": "input_image", "image_url": "https://example.com/a.png"},
                        {"type": "input_file", "file_id": "f"},
                    ],
                }
            ]
        }
    )
    response = _response(
        {"type": "web_search_call", "id": "ws_1", "action": {"type": "search", "query": "q"}},
        {"type": "function_call", "call_id": "c", "name": "f", "arguments": "not json"},
    )
    normalized = to_normalized(request, response)
    assert normalized.input.messages[0].content == [
        NormalizedContent(kind="image", media_type=MimeType("image/png"), byte_length=len(png)),
        NormalizedContent(kind="image"),
    ]
    assert normalized.output.message is not None
    assert normalized.output.message.content == [
        NormalizedContent(
            kind="tool_use",
            tool_use_id="ws_1",
            tool_name="web_search",
            tool_input={"type": "search", "query": "q"},
            tool_executor="server",
        ),
        NormalizedContent(
            kind="tool_use",
            tool_use_id="c",
            tool_name="f",
            tool_input="not json",
            tool_executor="client",
        ),
    ]
    assert normalized.output.stop_reason == "tool_use"


def test_incomplete_and_empty_output() -> None:
    response = Response.model_validate(
        {
            "object": "response",
            "id": "r",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        }
    )
    normalized = to_normalized(ResponsesRequest(input="hi"), response)
    assert normalized.output.message is None
    assert normalized.output.stop_reason == "max_tokens"


def test_round_links_across_a_compaction() -> None:
    request = ResponsesRequest.model_validate(
        {
            "input": [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "first"},
                {"type": "compaction", "encrypted_content": "summary"},
                {"role": "user", "content": "two"},
            ]
        }
    )
    response = _response(
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "x"}]}
    )
    normalized = to_normalized(request, response)
    assert normalized.output.message is not None
    messages = [*normalized.input.messages, normalized.output.message]
    assert [m.role for m in messages] == ["user", "assistant", "assistant", "user", "assistant"]
    own, hashes = round_links(messages, depth=10)
    compaction, _ = round_links(messages[:3], depth=10)
    previous, _ = round_links(messages[:2], depth=10)
    assert own and compaction and previous
    assert hashes == [own, compaction, previous, START]


async def test_build_event_requests_the_streamed_tool_call() -> None:
    normalized = await _stream_fixture()
    envelope = EventEnvelope(
        request_id="r",
        timestamp="2026-10-01T00:00:00Z",
        identity_details=OpenAIIdentityDetails(user_id="u"),
        model=AIModel(id="gpt"),
        parsed_as="openai-responses-stream",
    )
    event = await build_event_from_normalized(normalized, envelope, config=_CONFIG)
    assert event.requested_tool_uses == [
        AIToolUse(
            tool_id=normalized.input.tools_declared[0].id,
            tool_use_id="call_cee16a2c0447526b810260c5bad53c78",
        )
    ]
