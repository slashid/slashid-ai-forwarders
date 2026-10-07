"""DeepSeek R1 native InvokeModel bodies → NormalizedInvocation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.deepseek.normalize import (
    deepseek_stream_to_normalized_invocation,
    deepseek_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.deepseek.schema import (
    DeepSeekPromptRequest,
    DeepSeekRequest,
    DeepSeekResponse,
    DeepSeekStream,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedContent
from slashid_ai_forwarder_core.normalize.openai.chat.schema import (
    ChatCompletion,
    ChatRequest,
    ChatStream,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_REQUEST = TypeAdapter(DeepSeekRequest)
_STREAM = TypeAdapter(DeepSeekStream)
_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


async def _plain(name: str) -> Any:
    record = _load(name)
    return await deepseek_to_normalized_invocation(
        _REQUEST.validate_python(record["input"]["inputBodyJson"]),
        DeepSeekResponse.model_validate(record["output"]["outputBodyJson"]),
        config=_CONFIG,
    )


async def _streamed(name: str) -> Any:
    record = _load(name)
    return await deepseek_stream_to_normalized_invocation(
        _REQUEST.validate_python(record["input"]["inputBodyJson"]),
        _STREAM.validate_python(record["output"]["outputBodyJson"]),
        config=_CONFIG,
    )


def test_request_is_chat_or_prompt_shaped() -> None:
    chat = _REQUEST.validate_python(
        _load("invoke_deepseek_r1_chat_mil.json")["input"]["inputBodyJson"]
    )
    assert isinstance(chat, ChatRequest)
    prompt = _REQUEST.validate_python(
        _load("invoke_deepseek_r1_prompt_mil.json")["input"]["inputBodyJson"]
    )
    assert isinstance(prompt, DeepSeekPromptRequest)
    assert prompt.prompt == "Say hi in 3 words."


def test_response_shapes_validate_but_are_not_chat_completions() -> None:
    for name in ("invoke_deepseek_r1_chat_mil.json", "invoke_deepseek_r1_prompt_mil.json"):
        body = _load(name)["output"]["outputBodyJson"]
        assert DeepSeekResponse.model_validate(body).choices
        with pytest.raises(ValidationError):
            ChatCompletion.model_validate(body)
    for name in (
        "invoke_deepseek_r1_chat_stream_mil.json",
        "invoke_deepseek_r1_prompt_stream_mil.json",
    ):
        body = _load(name)["output"]["outputBodyJson"]
        assert _STREAM.validate_python(body)
        with pytest.raises(ValidationError):
            TypeAdapter(ChatStream).validate_python(body)


def test_other_shapes_are_rejected() -> None:
    chat = _load("openai_chat_trivial_mil.json")
    with pytest.raises(ValidationError):
        DeepSeekResponse.model_validate(chat["output"]["outputBodyJson"]["usage"])
    with pytest.raises(ValidationError):
        DeepSeekResponse.model_validate(chat["output"]["outputBodyJson"])
    with pytest.raises(ValidationError):
        _STREAM.validate_python(
            _load("openai_chat_trivial_stream_mil.json")["output"]["outputBodyJson"]
        )
    with pytest.raises(ValidationError):
        DeepSeekResponse.model_validate({"choices": []})
    with pytest.raises(ValidationError):
        _STREAM.validate_python([])
    with pytest.raises(ValidationError):
        _REQUEST.validate_python({"contents": []})


async def test_chat_shaped_record_keeps_the_reasoning() -> None:
    normalized = await _plain("invoke_deepseek_r1_chat_mil.json")
    assert [(m.role, m.content) for m in normalized.input.messages] == [
        ("user", [NormalizedContent(kind="text", text="Say hi in 3 words.")])
    ]
    assert normalized.output.message is not None
    (reasoning,) = normalized.output.message.content
    assert reasoning.kind == "reasoning"
    assert (reasoning.text or "").startswith("Okay, the user wants me to say")
    assert normalized.output.stop_reason == "max_tokens"


async def test_prompt_shaped_record() -> None:
    normalized = await _plain("invoke_deepseek_r1_prompt_mil.json")
    assert [(m.role, m.content) for m in normalized.input.messages] == [
        ("user", [NormalizedContent(kind="text", text="Say hi in 3 words.")])
    ]
    assert normalized.output.message is not None
    (answer,) = normalized.output.message.content
    assert (answer.kind, (answer.text or "")[:18]) == ("text", " You can do it. I ")
    assert normalized.output.stop_reason == "max_tokens"


@pytest.mark.parametrize("shape", ["chat", "prompt"])
async def test_stream_has_the_same_shape_as_the_plain_record(shape: str) -> None:
    plain = await _plain(f"invoke_deepseek_r1_{shape}_mil.json")
    streamed = await _streamed(f"invoke_deepseek_r1_{shape}_stream_mil.json")
    assert streamed.input == plain.input
    assert streamed.output.stop_reason == plain.output.stop_reason == "max_tokens"
    assert streamed.output.message is not None
    assert plain.output.message is not None
    assert [b.kind for b in streamed.output.message.content] == [
        b.kind for b in plain.output.message.content
    ]
