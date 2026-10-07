"""Providers that reuse one tool-call id (``call_0``) on every turn.

Grok and gpt-5.x on Bedrock return ``call_0`` for each call, in every
conversation. Pairing results to calls must still pick the latest call.
"""

from __future__ import annotations

from typing import Any

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import (
    AIModel,
    AIToolUse,
    EventEnvelope,
    OpenAIIdentityDetails,
    build_event_from_normalized,
    used_tools_of,
)
from slashid_ai_forwarder_core.normalize.normalized.tool_results import extract_tool_result_files
from slashid_ai_forwarder_core.normalize.openai.chat.normalize import to_normalized
from slashid_ai_forwarder_core.normalize.openai.chat.schema import ChatCompletion, ChatRequest

_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
    }


def _call(name: str, args: str, call_id: str = "call_0") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _assistant(*calls: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": None, "tool_calls": list(calls)}


def _result(text: str, call_id: str = "call_0") -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _normalize(messages: list[dict[str, Any]], *calls: dict[str, Any], tools: list[str]) -> Any:
    request = ChatRequest.model_validate({"messages": messages, "tools": [_tool(t) for t in tools]})
    response = ChatCompletion.model_validate(
        {
            "object": "chat.completion",
            "id": "c",
            "choices": [
                {
                    "finish_reason": "tool_calls" if calls else "stop",
                    "message": {"role": "assistant", "content": "done", "tool_calls": list(calls)},
                }
            ],
        }
    )
    return to_normalized(request, response)


def _tool_id(normalized: Any, name: str) -> str:
    return next(t.id for t in normalized.input.tools_declared if t.name == name)


_THREE_ROUNDS = [
    {"role": "user", "content": "weather, time, weather"},
    _assistant(_call("get_weather", '{"city":"Lisbon"}')),
    _result("sunny"),
    _assistant(_call("get_time", '{"city":"Lisbon"}')),
    _result("10:00"),
]


def test_used_tools_pairs_the_fresh_result_with_the_latest_call() -> None:
    normalized = _normalize(_THREE_ROUNDS, tools=["get_weather", "get_time"])
    assert used_tools_of(normalized) == [
        AIToolUse(tool_id=_tool_id(normalized, "get_time"), tool_use_id="call_0", is_error=False)
    ]


async def test_requested_tool_use_is_the_pending_call() -> None:
    normalized = _normalize(
        _THREE_ROUNDS, _call("get_weather", '{"city":"Tokyo"}'), tools=["get_weather", "get_time"]
    )
    envelope = EventEnvelope(
        request_id="r",
        timestamp="2026-10-07T00:00:00Z",
        identity_details=OpenAIIdentityDetails(user_id="u"),
        model=AIModel(id="grok"),
        parsed_as="openai-chat",
    )
    event = await build_event_from_normalized(normalized, envelope, config=_CONFIG)
    assert event.requested_tool_uses == [
        AIToolUse(tool_id=_tool_id(normalized, "get_weather"), tool_use_id="call_0")
    ]
    assert event.used_tools == [
        AIToolUse(tool_id=_tool_id(normalized, "get_time"), tool_use_id="call_0", is_error=False)
    ]


def test_accessed_file_comes_from_the_latest_call_not_an_earlier_one() -> None:
    normalized = _normalize(
        [
            {"role": "user", "content": "read a then b"},
            _assistant(_call("read_file", '{"path":"/repo/a.txt"}')),
            _result("contents of a"),
            _assistant(_call("read_file", '{"path":"/repo/b.txt"}')),
            _result("contents of b"),
        ],
        tools=["read_file"],
    )
    files = extract_tool_result_files(normalized.input.messages, config=_CONFIG)
    assert [f.name for f in files] == ["b.txt"]
