"""OpenAI Chat Completions → NormalizedInvocation."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIInvocationTokens
from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
)
from slashid_ai_forwarder_core.normalize.openai.chat.normalize import (
    chat_stream_to_normalized_invocation,
    chat_to_normalized_invocation,
    to_normalized,
)
from slashid_ai_forwarder_core.normalize.openai.chat.schema import (
    ChatCompletion,
    ChatRequest,
    ChatStream,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_STREAM = TypeAdapter(ChatStream)
_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


async def _fixture(name: str) -> NormalizedInvocation:
    record = _load(name)
    return await chat_to_normalized_invocation(
        ChatRequest.model_validate(record["input"]["inputBodyJson"]),
        ChatCompletion.model_validate(record["output"]["outputBodyJson"]),
        config=_CONFIG,
    )


async def _stream_fixture(name: str) -> NormalizedInvocation:
    record = _load(name)
    return await chat_stream_to_normalized_invocation(
        ChatRequest.model_validate(record["input"]["inputBodyJson"]),
        _STREAM.validate_python(record["output"]["outputBodyJson"]),
        config=_CONFIG,
    )


def _text(text: str) -> NormalizedContent:
    return NormalizedContent(kind="text", text=text)


def _invocation(
    messages: list[dict[str, Any]], assistant: dict[str, Any], finish: str = "stop"
) -> NormalizedInvocation:
    return to_normalized(
        ChatRequest.model_validate({"messages": messages}),
        ChatCompletion.model_validate(
            {
                "object": "chat.completion",
                "id": "c",
                "choices": [
                    {"finish_reason": finish, "message": {"role": "assistant", **assistant}}
                ],
            }
        ),
    )


async def test_plain_fixture() -> None:
    normalized = await _fixture("openai_chat_trivial_mil.json")
    assert [(m.role, m.content) for m in normalized.input.messages] == [
        ("user", [_text("Say hi in 3 words.")])
    ]
    assert normalized.output.message is not None
    assert normalized.output.message.role == "assistant"
    assert normalized.output.message.content == [_text("Hi there, friend!")]
    assert normalized.output.stop_reason == "end_turn"
    assert normalized.tokens == AIInvocationTokens(input=13, output=11, reasoning=19)


async def test_system_and_developer_messages_merge_into_one_system_message() -> None:
    normalized = await _fixture("openai_chat_system_developer_mil.json")
    assert [(m.role, m.content) for m in normalized.input.messages] == [
        ("system", [_text("Answer in French."), _text("Be terse.")]),
        ("user", [_text("What is 2+2?")]),
    ]


async def test_inline_reasoning_tags_become_a_reasoning_block() -> None:
    normalized = await _fixture("openai_chat_thinking_oss_low_mil.json")
    assert normalized.output.message is not None
    assert normalized.output.message.content == [
        NormalizedContent(kind="reasoning", text="Simple multiplication. 17*23 = 391."),
        _text("\\(17 \\times 23 = 391\\)"),
    ]


def test_reasoning_tags_without_trailing_text_or_closing_tag() -> None:
    only = _invocation([{"role": "user", "content": "q"}], {"content": "<reasoning>a</reasoning>"})
    assert only.output.message is not None
    assert only.output.message.content == [NormalizedContent(kind="reasoning", text="a")]
    cut = _invocation([{"role": "user", "content": "q"}], {"content": "<reasoning>unfinished"})
    assert cut.output.message is not None
    assert cut.output.message.content == [NormalizedContent(kind="reasoning", text="unfinished")]


def test_reasoning_tags_in_assistant_history_are_split_too() -> None:
    normalized = _invocation(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "<reasoning>r</reasoning>answer"},
            {"role": "user", "content": "more"},
        ],
        {"content": "ok"},
    )
    assert normalized.input.messages[1].content == [
        NormalizedContent(kind="reasoning", text="r"),
        _text("answer"),
    ]


def test_reasoning_tags_in_user_text_are_left_alone() -> None:
    normalized = _invocation(
        [{"role": "user", "content": "<reasoning>not mine</reasoning>"}], {"content": "ok"}
    )
    assert normalized.input.messages[0].content == [_text("<reasoning>not mine</reasoning>")]


async def test_null_content_yields_no_output_message() -> None:
    normalized = await _fixture("openai_chat_astra_length_mil.json")
    assert normalized.output.message is None
    assert normalized.output.stop_reason == "max_tokens"
    assert normalized.tokens == AIInvocationTokens(input=14, output=0, reasoning=20)


async def test_tool_call_response() -> None:
    normalized = await _fixture("openai_chat_tool_call_mil.json")
    assert [t.name for t in normalized.input.tools_declared] == ["get_weather"]
    assert normalized.input.tools_declared[0].input_schema is not None
    assert normalized.output.stop_reason == "tool_use"
    assert normalized.output.message is not None
    reasoning, call = normalized.output.message.content
    assert reasoning.kind == "reasoning"
    assert call == NormalizedContent(
        kind="tool_use",
        tool_use_id=call.tool_use_id,
        tool_name="get_weather",
        tool_input={"city": "Lisbon"},
        tool_executor="client",
    )
    assert call.tool_use_id


async def test_tool_call_history_and_result() -> None:
    normalized = await _fixture("openai_chat_tool_result_turn_mil.json")
    assert [(m.role, [b.kind for b in m.content]) for m in normalized.input.messages] == [
        ("user", ["text"]),
        ("assistant", ["tool_use"]),
        ("user", ["tool_result"]),
    ]
    use = normalized.input.messages[1].content[0]
    assert (use.tool_use_id, use.tool_name, use.tool_input) == (
        "call_1",
        "get_weather",
        {"city": "Lisbon"},
    )
    assert normalized.input.messages[2].content[0] == NormalizedContent(
        kind="tool_result",
        tool_use_id="call_1",
        tool_output='{"temp_c": 22, "sky": "sunny"}',
        tool_executor="client",
    )


async def test_parallel_tool_results_share_one_user_message() -> None:
    normalized = await _fixture("openai_chat_tool_result_parallel_turn_mil.json")
    assert [(m.role, len(m.content)) for m in normalized.input.messages] == [
        ("user", 1),
        ("assistant", 4),
        ("user", 4),
    ]
    assert [b.tool_use_id for b in normalized.input.messages[2].content] == [
        "call_a",
        "call_b",
        "call_c",
        "call_d",
    ]


def test_unparseable_tool_arguments_are_kept_raw() -> None:
    normalized = _invocation(
        [{"role": "user", "content": "q"}],
        {
            "content": None,
            "tool_calls": [
                {"id": "t", "type": "function", "function": {"name": "f", "arguments": "{oops"}}
            ],
        },
        finish="tool_calls",
    )
    assert normalized.output.message is not None
    assert normalized.output.message.content[0].tool_input == "{oops"


async def test_image_part() -> None:
    normalized = await _fixture("openai_chat_image_data_url_mil.json")
    url = _load("openai_chat_image_data_url_mil.json")["input"]["inputBodyJson"]["messages"][0][
        "content"
    ][1]["image_url"]["url"]
    text, image = normalized.input.messages[0].content
    assert text.kind == "text"
    assert (image.kind, str(image.media_type)) == ("image", "image/png")
    assert image.byte_length == len(base64.b64decode(url.partition(",")[2]))


async def test_file_part_is_a_document() -> None:
    normalized = await _fixture("openai_chat_file_pdf_data_mil.json")
    _, doc = normalized.input.messages[0].content
    assert (doc.kind, str(doc.media_type)) == ("document", "application/pdf")
    assert doc.byte_length is not None
    assert doc.byte_length > 0


def test_unknown_part_is_skipped_and_refusal_is_text() -> None:
    normalized = _invocation(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "input_audio", "input_audio": {"data": "x"}},
                ],
            }
        ],
        {"content": None, "refusal": "I can't."},
    )
    assert normalized.input.messages[0].content == [_text("hi")]
    assert normalized.output.message is not None
    assert normalized.output.message.content == [_text("I can't.")]


async def test_cached_prompt_tokens() -> None:
    normalized = await _fixture("openai_chat_large_prompt_repeat_cached_mil.json")
    assert normalized.tokens == AIInvocationTokens(input=2, cache_read=24316, output=5)


@pytest.mark.parametrize("name", ["trivial", "tool_call", "thinking_oss_low", "astra_length"])
async def test_stream_matches_non_stream(name: str) -> None:
    streamed = await _stream_fixture(f"openai_chat_{name}_stream_mil.json")
    plain = await _fixture(f"openai_chat_{name}_mil.json")
    assert streamed.output.stop_reason == plain.output.stop_reason
    assert [
        b.kind for b in (streamed.output.message.content if streamed.output.message else [])
    ] == [b.kind for b in (plain.output.message.content if plain.output.message else [])]
    assert streamed.input == plain.input


async def test_stream_tokens_come_from_the_usage_chunk() -> None:
    normalized = await _stream_fixture("openai_chat_trivial_stream_mil.json")
    assert normalized.tokens.input == 13
    assert normalized.tokens.output + normalized.tokens.reasoning == 29


async def test_stream_missing_usage_still_normalizes() -> None:
    record = _load("openai_chat_trivial_stream_mil.json")
    chunks = [c for c in record["output"]["outputBodyJson"] if c.get("choices")]
    normalized = await chat_stream_to_normalized_invocation(
        ChatRequest.model_validate(record["input"]["inputBodyJson"]),
        _STREAM.validate_python(chunks),
        config=_CONFIG,
    )
    assert normalized.tokens == AIInvocationTokens()
    assert normalized.output.stop_reason == "end_turn"


async def test_reasoning_content_field_becomes_a_leading_reasoning_block() -> None:
    normalized = await _fixture("openai_chat_kimi_k3_mil.json")
    assert normalized.output.message is not None
    reasoning, text = normalized.output.message.content
    assert (reasoning.kind, (reasoning.text or "")[:14]) == ("reasoning", "The user asked")
    assert text == _text("Hi there, friend! 👋")
    streamed = await _stream_fixture("openai_chat_kimi_k3_stream_mil.json")
    assert streamed.output.message is not None
    assert [b.kind for b in streamed.output.message.content] == ["reasoning", "text"]


def test_reasoning_content_in_assistant_history() -> None:
    normalized = _invocation(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a", "reasoning_content": "why"},
            {"role": "user", "content": "more"},
        ],
        {"content": "ok"},
    )
    assert normalized.input.messages[1].content == [
        NormalizedContent(kind="reasoning", text="why"),
        _text("a"),
    ]


async def test_message_fragment_stream_matches_non_stream() -> None:
    plain = await _fixture("invoke_pixtral_chat_mil.json")
    streamed = await _stream_fixture("invoke_pixtral_chat_stream_mil.json")
    assert plain.output.message is not None
    assert plain.output.message.content == [_text("Hi there!")]
    assert streamed.output.message == plain.output.message
    assert (plain.output.stop_reason, streamed.output.stop_reason) == ("end_turn", "end_turn")
    assert streamed.tokens == plain.tokens == AIInvocationTokens(input=10, output=4)
