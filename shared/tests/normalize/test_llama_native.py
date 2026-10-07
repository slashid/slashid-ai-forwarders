"""Meta Llama native InvokeModel bodies → NormalizedInvocation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIInvocationTokens
from slashid_ai_forwarder_core.normalize.llama.normalize import (
    llama_stream_to_normalized_invocation,
    llama_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.llama.schema import (
    LlamaRequest,
    LlamaResponse,
    LlamaStream,
    accumulate_stream,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedContent

_FIXTURES = Path(__file__).parent / "fixtures"
_STREAM = TypeAdapter(LlamaStream)
_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def test_fixtures_validate() -> None:
    record = _load("invoke_llama_native_mil.json")
    request = LlamaRequest.model_validate(record["input"]["inputBodyJson"])
    assert request.prompt.startswith("<|begin_of_text|>")
    response = LlamaResponse.model_validate(record["output"]["outputBodyJson"])
    assert (response.generation, response.stop_reason) == ("Hello to you.", "stop")
    assert (response.prompt_token_count, response.generation_token_count) == (17, 5)


def test_stream_accumulates_text_counts_and_stop_reason() -> None:
    record = _load("invoke_llama_native_stream_mil.json")
    final = accumulate_stream(_STREAM.validate_python(record["output"]["outputBodyJson"]))
    assert final is not None
    assert final.generation == "Hello to you."
    assert (final.prompt_token_count, final.generation_token_count) == (17, 5)
    assert final.stop_reason == "stop"


def test_shapes_do_not_cross_validate() -> None:
    chat = _load("openai_chat_trivial_mil.json")
    with pytest.raises(ValidationError):
        LlamaRequest.model_validate(chat["input"]["inputBodyJson"])
    with pytest.raises(ValidationError):
        LlamaResponse.model_validate(chat["output"]["outputBodyJson"])
    with pytest.raises(ValidationError):
        _STREAM.validate_python([])
    with pytest.raises(ValidationError):
        _STREAM.validate_python(chat["output"]["outputBodyJson"]["choices"])
    assert accumulate_stream([]) is None


async def test_plain_record() -> None:
    record = _load("invoke_llama_native_mil.json")
    normalized = await llama_to_normalized_invocation(
        LlamaRequest.model_validate(record["input"]["inputBodyJson"]),
        LlamaResponse.model_validate(record["output"]["outputBodyJson"]),
        config=_CONFIG,
    )
    (message,) = normalized.input.messages
    assert message.role == "user"
    assert message.content == [
        NormalizedContent(kind="text", text=record["input"]["inputBodyJson"]["prompt"])
    ]
    assert normalized.output.message is not None
    assert normalized.output.message.role == "assistant"
    assert normalized.output.message.content == [
        NormalizedContent(kind="text", text="Hello to you.")
    ]
    assert normalized.output.stop_reason == "end_turn"
    assert normalized.tokens == AIInvocationTokens(input=17, output=5)


async def test_stream_matches_the_plain_record() -> None:
    plain = _load("invoke_llama_native_mil.json")
    stream = _load("invoke_llama_native_stream_mil.json")
    request = LlamaRequest.model_validate(stream["input"]["inputBodyJson"])
    streamed = await llama_stream_to_normalized_invocation(
        request, _STREAM.validate_python(stream["output"]["outputBodyJson"]), config=_CONFIG
    )
    expected = await llama_to_normalized_invocation(
        LlamaRequest.model_validate(plain["input"]["inputBodyJson"]),
        LlamaResponse.model_validate(plain["output"]["outputBodyJson"]),
        config=_CONFIG,
    )
    assert streamed.output == expected.output
    assert streamed.input == expected.input
    assert streamed.tokens == expected.tokens


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("stop", "end_turn"), ("length", "max_tokens"), ("other", "unknown"), (None, "unknown")],
)
async def test_stop_reasons(stop_reason: str | None, expected: str) -> None:
    normalized = await llama_to_normalized_invocation(
        LlamaRequest(prompt="p"),
        LlamaResponse(generation="x", stop_reason=stop_reason),
        config=_CONFIG,
    )
    assert normalized.output.stop_reason == expected


async def test_empty_generation_yields_no_output_message() -> None:
    normalized = await llama_to_normalized_invocation(
        LlamaRequest(prompt="p"), LlamaResponse(generation="", stop_reason="length"), config=_CONFIG
    )
    assert normalized.output.message is None
    assert normalized.output.stop_reason == "max_tokens"
