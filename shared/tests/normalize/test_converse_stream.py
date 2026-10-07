"""Converse stream events (Nova's native InvokeModelWithResponseStream) → NormalizedInvocation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.converse.normalize import (
    to_normalized_invocation,
    to_stream_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseReasoningBlock,
    ConverseRequestBody,
    ConverseResponse,
    ConverseTextBlock,
    ConverseToolUseBlock,
)
from slashid_ai_forwarder_core.normalize.converse.stream import (
    ConverseStream,
    accumulate_stream,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_STREAM = TypeAdapter(ConverseStream)
_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def _accumulated(name: str) -> ConverseResponse:
    final = accumulate_stream(_STREAM.validate_python(_load(name)["output"]["outputBodyJson"]))
    assert final is not None
    return final


def _events(*events: dict[str, Any]) -> list[Any]:
    return _STREAM.validate_python(
        [
            {"messageStart": {"role": "assistant"}},
            *events,
            {"messageStop": {"stopReason": "end_turn"}},
        ]
    )


def _delta(index: int, **delta: Any) -> dict[str, Any]:
    return {"contentBlockDelta": {"contentBlockIndex": index, "delta": delta}}


_SCENARIOS = [
    "plain",
    "system_multiturn",
    "tool_call",
    "tool_parallel",
    "tool_result_turn",
    "max_tokens",
    "image",
    "thinking_native",
]


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_real_streams_validate_and_plain_records_do_not(scenario: str) -> None:
    stream = _load(f"invoke_nova_{scenario}_stream_mil.json")["output"]["outputBodyJson"]
    assert _STREAM.validate_python(stream)
    plain = _load(f"invoke_nova_{scenario}_mil.json")["output"]["outputBodyJson"]
    with pytest.raises(ValidationError):
        _STREAM.validate_python(plain)
    with pytest.raises(ValidationError):
        ConverseResponse.model_validate(stream)


def test_other_formats_streams_are_rejected() -> None:
    for name in (
        "openai_chat_trivial_stream_mil.json",
        "invoke_llama_native_stream_mil.json",
        "invoke_deepseek_r1_chat_stream_mil.json",
        "openai_responses_stream_mil.json",
    ):
        with pytest.raises(ValidationError):
            _STREAM.validate_python(_load(name)["output"]["outputBodyJson"])
    with pytest.raises(ValidationError):
        _STREAM.validate_python([])
    with pytest.raises(ValidationError):
        _STREAM.validate_python([{"contentBlockDelta": {"nonsense": 1}, "other": 1}, {"x": 1}])
    assert accumulate_stream([]) is None


def test_text_stream_accumulates() -> None:
    final = _accumulated("invoke_nova_plain_stream_mil.json")
    assert final.stopReason == "end_turn"
    (block,) = final.output.message.content
    assert isinstance(block, ConverseTextBlock)
    assert block.text
    assert isinstance(final.usage, dict)
    assert final.usage["inputTokens"] == 7


def test_tool_use_input_fragments_are_joined_and_parsed() -> None:
    final = _accumulated("invoke_nova_tool_call_stream_mil.json")
    assert final.stopReason == "tool_use"
    tool_uses = [b for b in final.output.message.content if isinstance(b, ConverseToolUseBlock)]
    assert [(t.toolUse.name, t.toolUse.input) for t in tool_uses] == [
        ("get_weather", {"city": "Lisbon"})
    ]
    assert tool_uses[0].toolUse.toolUseId


def test_parallel_tool_uses_stay_apart() -> None:
    final = _accumulated("invoke_nova_tool_parallel_stream_mil.json")
    tool_uses = [
        b.toolUse for b in final.output.message.content if isinstance(b, ConverseToolUseBlock)
    ]
    assert len(tool_uses) == 2
    assert len({t.toolUseId for t in tool_uses}) == 2
    assert sorted(t.name for t in tool_uses) == ["get_time", "get_weather"]
    assert all(t.input == {"city": "Lisbon"} for t in tool_uses)


def test_reasoning_then_text_in_block_order() -> None:
    final = _accumulated("invoke_nova_thinking_native_stream_mil.json")
    kinds = [type(b) for b in final.output.message.content]
    assert kinds == [ConverseReasoningBlock, ConverseTextBlock]
    reasoning = final.output.message.content[0]
    assert isinstance(reasoning, ConverseReasoningBlock)
    assert reasoning.reasoningContent.reasoningText is not None


def test_max_tokens_stop_reason() -> None:
    assert _accumulated("invoke_nova_max_tokens_stream_mil.json").stopReason == "max_tokens"


def test_blocks_are_ordered_by_index_not_arrival() -> None:
    final = accumulate_stream(
        _events(_delta(1, text="second"), _delta(0, text="first "), _delta(1, text=" half"))
    )
    assert final is not None
    (block,) = final.output.message.content
    assert isinstance(block, ConverseTextBlock)
    assert block.text == "first second half"


def test_a_tool_use_separates_text_blocks() -> None:
    tool = {
        "contentBlockStart": {
            "contentBlockIndex": 1,
            "start": {"toolUse": {"toolUseId": "t", "name": "f"}},
        }
    }
    final = accumulate_stream(
        _events(
            _delta(0, text="before"),
            tool,
            _delta(1, toolUse={"input": "{}"}),
            _delta(2, text="after"),
        )
    )
    assert final is not None
    assert [type(b) for b in final.output.message.content] == [
        ConverseTextBlock,
        ConverseToolUseBlock,
        ConverseTextBlock,
    ]


def test_a_signature_closes_a_reasoning_block() -> None:
    final = accumulate_stream(
        _events(
            _delta(0, reasoningContent={"text": "one "}),
            _delta(1, reasoningContent={"text": "two"}),
            _delta(1, reasoningContent={"signature": "sig"}),
            _delta(2, reasoningContent={"text": "three"}),
        )
    )
    assert final is not None
    texts = [
        b.reasoningContent.reasoningText.text
        for b in final.output.message.content
        if isinstance(b, ConverseReasoningBlock) and b.reasoningContent.reasoningText
    ]
    assert texts == ["one two", "three"]


def test_tool_use_edge_cases() -> None:
    def start(index: int, name: str) -> dict[str, Any]:
        return {
            "contentBlockStart": {
                "contentBlockIndex": index,
                "start": {"toolUse": {"toolUseId": f"id{index}", "name": name}},
            }
        }

    final = accumulate_stream(
        _events(
            start(0, "no_args"),
            start(1, "broken"),
            _delta(1, toolUse={"input": "{oops"}),
            start(2, "split"),
            _delta(2, toolUse={"input": '{"a":'}),
            _delta(2, toolUse={"input": " 1}"}),
        )
    )
    assert final is not None
    uses = {
        b.toolUse.name: b.toolUse.input
        for b in final.output.message.content
        if isinstance(b, ConverseToolUseBlock)
    }
    assert uses == {"no_args": {}, "broken": "{oops", "split": {"a": 1}}


def test_reasoning_signature_and_redacted_content() -> None:
    final = accumulate_stream(
        _events(
            _delta(0, reasoningContent={"text": "think "}),
            _delta(0, reasoningContent={"text": "hard"}),
            _delta(0, reasoningContent={"signature": "sig=="}),
            _delta(1, reasoningContent={"redactedContent": "cmVk"}),
        )
    )
    assert final is not None
    first, second = final.output.message.content
    assert isinstance(first, ConverseReasoningBlock)
    assert first.reasoningContent.reasoningText is not None
    assert (
        first.reasoningContent.reasoningText.text,
        first.reasoningContent.reasoningText.signature,
    ) == (
        "think hard",
        "sig==",
    )
    assert isinstance(second, ConverseReasoningBlock)
    assert second.reasoningContent.redactedContent == "cmVk"


def test_stream_without_stop_or_metadata() -> None:
    final = accumulate_stream(
        _STREAM.validate_python(
            [{"messageStart": {"role": "assistant"}}, _delta(0, text="cut off")]
        )
    )
    assert final is not None
    assert final.stopReason is None
    assert final.usage is None


@pytest.mark.parametrize("scenario", _SCENARIOS)
async def test_stream_normalizes_like_the_plain_record(scenario: str) -> None:
    plain = _load(f"invoke_nova_{scenario}_mil.json")
    stream = _load(f"invoke_nova_{scenario}_stream_mil.json")
    request = ConverseRequestBody.model_validate(stream["input"]["inputBodyJson"])
    streamed = await to_stream_normalized_invocation(
        request, _STREAM.validate_python(stream["output"]["outputBodyJson"]), config=_CONFIG
    )
    expected = await to_normalized_invocation(
        ConverseRequestBody.model_validate(plain["input"]["inputBodyJson"]),
        ConverseResponse.model_validate(plain["output"]["outputBodyJson"]),
        config=_CONFIG,
    )
    assert streamed.input == expected.input
    assert streamed.accessed_files == expected.accessed_files
    assert streamed.output.stop_reason == expected.output.stop_reason
    assert streamed.output.message is not None
    assert expected.output.message is not None
    assert [b.kind for b in streamed.output.message.content] == [
        b.kind for b in expected.output.message.content
    ]
