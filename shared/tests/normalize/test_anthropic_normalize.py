"""Pure-function transforms: Anthropic -> Converse."""

from __future__ import annotations

import logging

import pytest
from pydantic import TypeAdapter

from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    extract_stream_usage,
    message_to_converse,
    stream_to_converse,
    tools_to_converse_tool_config,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicMessage,
    AnthropicStreamEvent,
    AnthropicToolDeclaration,
    AnthropicUsage,
)
from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseResponse,
    ConverseToolConfig,
)
from slashid_ai_forwarder_core.testing import yaml_pytest

_STREAM = TypeAdapter(list[AnthropicStreamEvent])


# --------------------------------------------------------------------------
# message_to_converse
# --------------------------------------------------------------------------


@yaml_pytest()
def test_anthropic_message_to_converse(
    body: AnthropicMessage,
    expected: ConverseResponse,
) -> None:
    assert message_to_converse(body) == expected


def test_anthropic_message_tool_use_with_null_input_becomes_empty_dict() -> None:
    # Byte-parity with legacy: falsy input (None or absent) becomes {}.
    msg = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_x", "name": "noop"},
            ],
            "stop_reason": "tool_use",
        }
    )
    result = message_to_converse(msg)
    assert result.model_dump(exclude_none=True) == {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": "toolu_x", "name": "noop", "input": {}}},
                ],
            },
        },
        "stopReason": "tool_use",
    }


# --------------------------------------------------------------------------
# stream_to_converse
# --------------------------------------------------------------------------


@yaml_pytest()
def test_anthropic_stream_to_converse(
    body: list[AnthropicStreamEvent],
    expected: ConverseResponse,
) -> None:
    assert stream_to_converse(body) == expected


def test_anthropic_stream_malformed_input_json_yields_empty_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A partial_json that never assembles into valid JSON should log a
    # warning with byte count and produce {} — matches Phase 1 behaviour.
    events = _STREAM.validate_python(
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "not json"},
            },
            {"type": "content_block_stop", "index": 0},
        ]
    )
    with caplog.at_level(
        logging.WARNING,
        logger="slashid_ai_forwarder_core.normalize.anthropic.normalize",
    ):
        result = stream_to_converse(events)
    block = result.output.message.content[0]
    # Grab the toolUse block's input via model_dump for symmetry.
    dumped = block.model_dump(exclude_none=True)
    assert dumped == {"toolUse": {"toolUseId": "toolu_1", "name": "read", "input": {}}}
    assert any("tool_use input_json malformed" in r.message for r in caplog.records)
    # The byte count is present in the fully-formatted log message.
    assert any("8 bytes" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# tools_to_converse_tool_config
# --------------------------------------------------------------------------


@yaml_pytest()
def test_anthropic_tools_to_converse_tool_config(
    body: list[AnthropicToolDeclaration],
    expected: ConverseToolConfig,
) -> None:
    assert tools_to_converse_tool_config(body) == expected


# --------------------------------------------------------------------------
# extract_stream_usage
# --------------------------------------------------------------------------


@yaml_pytest()
def test_extract_stream_usage(
    body: list[AnthropicStreamEvent],
    expected: AnthropicUsage,
) -> None:
    assert extract_stream_usage(body) == expected
