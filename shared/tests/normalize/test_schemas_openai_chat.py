"""OpenAI Chat Completions wire-schema validation against Bedrock MIL captures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from slashid_ai_forwarder_core.normalize.openai.chat.schema import (
    ChatCompletion,
    ChatFilePart,
    ChatImagePart,
    ChatMessage,
    ChatRequest,
    ChatStream,
    ChatTextPart,
    ChatUnknownPart,
    accumulate_stream,
)
from slashid_ai_forwarder_core.normalize.openai.responses.schema import Response, ResponsesRequest

_FIXTURES = Path(__file__).parent / "fixtures"
_STREAM = TypeAdapter(ChatStream)


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def test_plain_fixture_validates() -> None:
    record = _load("openai_chat_trivial_mil.json")
    request = ChatRequest.model_validate(record["input"]["inputBodyJson"])
    assert [(m.role, m.content) for m in request.messages] == [("user", "Say hi in 3 words.")]
    response = ChatCompletion.model_validate(record["output"]["outputBodyJson"])
    assert response.object == "chat.completion"
    assert response.choices[0].finish_reason == "stop"
    assert response.choices[0].message.content == "Hi there, friend!"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 13


def test_tool_call_response_validates() -> None:
    record = _load("openai_chat_tool_call_mil.json")
    request = ChatRequest.model_validate(record["input"]["inputBodyJson"])
    function = request.tools[0].function
    assert function is not None
    assert function.name == "get_weather"
    assert function.parameters is not None
    response = ChatCompletion.model_validate(record["output"]["outputBodyJson"])
    choice = response.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert [(c.function.name, c.function.arguments) for c in choice.message.tool_calls] == [
        ("get_weather", '{\n  "city": "Lisbon"\n}')
    ]


def test_tool_result_request_validates() -> None:
    record = _load("openai_chat_tool_result_turn_mil.json")
    request = ChatRequest.model_validate(record["input"]["inputBodyJson"])
    assistant, tool = request.messages[1], request.messages[2]
    assert assistant.content is None
    assert assistant.tool_calls[0].id == "call_1"
    assert (tool.role, tool.tool_call_id) == ("tool", "call_1")


def test_image_and_file_parts_validate() -> None:
    image = ChatRequest.model_validate(
        _load("openai_chat_image_data_url_mil.json")["input"]["inputBodyJson"]
    )
    parts = image.messages[0].content
    assert isinstance(parts, list)
    assert [type(p) for p in parts] == [ChatTextPart, ChatImagePart]
    assert isinstance(parts[1], ChatImagePart)
    assert parts[1].image_url.url.startswith("data:image/png;base64,")
    pdf = ChatRequest.model_validate(
        _load("openai_chat_file_pdf_data_mil.json")["input"]["inputBodyJson"]
    )
    parts = pdf.messages[0].content
    assert isinstance(parts, list)
    assert isinstance(parts[1], ChatFilePart)
    assert parts[1].file.filename == "secret.pdf"
    assert (parts[1].file.file_data or "").startswith("data:application/pdf;base64,")


def test_unknown_part_type_is_kept() -> None:
    msg = ChatMessage.model_validate(
        {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "x"}}]}
    )
    assert isinstance(msg.content, list)
    assert isinstance(msg.content[0], ChatUnknownPart)


def test_null_content_with_refusal() -> None:
    record = _load("openai_chat_astra_length_mil.json")
    response = ChatCompletion.model_validate(record["output"]["outputBodyJson"])
    assert response.choices[0].finish_reason == "length"
    assert response.choices[0].message.content is None
    msg = ChatMessage.model_validate({"role": "assistant", "content": None, "refusal": "no"})
    assert msg.refusal == "no"


def test_usage_details() -> None:
    record = _load("openai_chat_large_prompt_repeat_cached_mil.json")
    usage = ChatCompletion.model_validate(record["output"]["outputBodyJson"]).usage
    assert usage is not None
    assert usage.prompt_tokens_details is not None
    assert usage.prompt_tokens_details.cached_tokens == 24316
    reasoning = ChatCompletion.model_validate(
        _load("openai_chat_thinking_high_mil.json")["output"]["outputBodyJson"]
    ).usage
    assert reasoning is not None
    assert reasoning.completion_tokens_details is not None
    assert reasoning.completion_tokens_details.reasoning_tokens == 97


def test_chat_and_responses_shapes_do_not_cross_validate() -> None:
    responses = _load("openai_responses_mil.json")
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(responses["input"]["inputBodyJson"])
    with pytest.raises(ValidationError):
        ChatCompletion.model_validate(responses["output"]["outputBodyJson"])
    chat = _load("openai_chat_trivial_mil.json")
    with pytest.raises(ValidationError):
        ResponsesRequest.model_validate(chat["input"]["inputBodyJson"])
    with pytest.raises(ValidationError):
        Response.model_validate(chat["output"]["outputBodyJson"])


def test_non_function_tool_does_not_fail_the_request() -> None:
    request = ChatRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"type": "custom", "custom": {"name": "apply_patch"}},
                {"type": "function", "function": {"name": "f"}},
            ],
        }
    )
    assert [t.type for t in request.tools] == ["custom", "function"]
    assert [t.function.name if t.function else None for t in request.tools] == [None, "f"]


def _accumulated(name: str) -> ChatCompletion:
    record = _load(name)
    final = accumulate_stream(_STREAM.validate_python(record["output"]["outputBodyJson"]))
    assert final is not None
    return final


def test_stream_accumulates_text_and_usage() -> None:
    final = _accumulated("openai_chat_trivial_stream_mil.json")
    assert final.choices[0].message.role == "assistant"
    assert final.choices[0].message.content == "Hi there, friend!"
    assert final.choices[0].finish_reason == "stop"
    assert final.usage is not None
    assert (final.usage.prompt_tokens, final.usage.completion_tokens) == (13, 29)
    assert final.usage.completion_tokens_details is not None


def test_stream_merges_tool_call_arguments() -> None:
    final = _accumulated("openai_chat_tool_call_stream_mil.json")
    calls = final.choices[0].message.tool_calls
    assert [(c.function.name, json.loads(c.function.arguments)) for c in calls] == [
        ("get_weather", {"city": "Lisbon"})
    ]
    assert calls[0].id
    assert final.choices[0].finish_reason == "tool_calls"


def test_stream_keeps_parallel_tool_calls_apart() -> None:
    # Hand-built: gpt-oss on Bedrock only ever emits one call per turn.
    def chunk(*calls: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": "c",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": list(calls)}}],
        }

    def call(index: int, **fn: str) -> dict[str, Any]:
        return {
            "index": index,
            **({"id": f"call_{index}", "type": "function"} if "name" in fn else {}),
            "function": fn,
        }

    final = accumulate_stream(
        _STREAM.validate_python(
            [
                chunk(call(0, name="get_weather", arguments="")),
                chunk(call(0, arguments='{"city":')),
                chunk(call(1, name="get_time", arguments="")),
                chunk(call(0, arguments='"Lisbon"}'), call(1, arguments='{"city": "Tokyo"}')),
            ]
        )
    )
    assert final is not None
    assert [
        (c.id, c.function.name, json.loads(c.function.arguments))
        for c in final.choices[0].message.tool_calls
    ] == [
        ("call_0", "get_weather", {"city": "Lisbon"}),
        ("call_1", "get_time", {"city": "Tokyo"}),
    ]


def test_stream_with_only_reasoning_has_null_content() -> None:
    final = _accumulated("openai_chat_astra_length_stream_mil.json")
    assert final.choices[0].message.content is None
    assert final.choices[0].finish_reason == "length"


def test_stream_without_usage_chunk() -> None:
    chunks = _STREAM.validate_python(
        [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "a"}}],
            },
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
    )
    final = accumulate_stream(chunks)
    assert final is not None
    assert final.usage is None
    assert final.choices[0].message.content == "a"


def test_stream_schema_rejects_empty_and_foreign_lists() -> None:
    with pytest.raises(ValidationError):
        _STREAM.validate_python([])
    with pytest.raises(ValidationError):
        _STREAM.validate_python([{"type": "response.completed"}])
    assert accumulate_stream([]) is None


def test_reasoning_content_field_is_read_and_accumulated() -> None:
    plain = ChatCompletion.model_validate(
        _load("openai_chat_kimi_k3_mil.json")["output"]["outputBodyJson"]
    )
    reasoning = plain.choices[0].message.reasoning_content
    assert reasoning is not None
    assert reasoning.startswith("The user asked")
    streamed = _accumulated("openai_chat_kimi_k3_stream_mil.json")
    assert streamed.choices[0].message.reasoning_content
    content = streamed.choices[0].message.content
    assert isinstance(content, str)
    assert content.startswith("Hi there")


def test_explicit_null_list_fields_are_tolerated() -> None:
    record = _load("invoke_pixtral_chat_mil.json")
    response = ChatCompletion.model_validate(record["output"]["outputBodyJson"])
    assert response.choices[0].message.content == "Hi there!"
    assert response.choices[0].message.tool_calls == []


def test_stream_chunks_with_message_fragments_and_stop_reason() -> None:
    final = _accumulated("invoke_pixtral_chat_stream_mil.json")
    assert final.choices[0].message.content == "Hi there!"
    assert final.choices[0].finish_reason == "stop"
    assert final.usage is not None
    assert (final.usage.prompt_tokens, final.usage.completion_tokens) == (10, 4)
