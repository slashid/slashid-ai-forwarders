"""Pure-function transforms: Anthropic-side helpers used by mil_normalize."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    extract_stream_usage,
    tools_to_converse_tool_config,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicStreamEvent,
    AnthropicToolDeclaration,
    AnthropicUsage,
)
from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseToolConfig,
)
from slashid_ai_forwarder_core.testing import yaml_pytest

# --------------------------------------------------------------------------
# Behaviour invariants for the Anthropic → NormalizedInvocation translates
# --------------------------------------------------------------------------


def test_anthropic_message_tool_use_with_null_input_becomes_empty_dict() -> None:
    """Byte-parity: falsy tool_use input (None or absent) becomes {} in the
    canonical NormalizedContent. Matters because model_dump of {} differs
    from model_dump of None on the wire — content-hash stability depends
    on it."""
    from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
        message_to_normalized_invocation,
    )
    from slashid_ai_forwarder_core.normalize.anthropic.schema import (
        AnthropicMessage,
        AnthropicRequestBody,
    )

    request = AnthropicRequestBody.model_validate({
        "messages": [{"role": "user", "content": [{"type": "text", "text": "noop"}]}],
    })
    response = AnthropicMessage.model_validate({
        "type": "message",
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "toolu_x", "name": "noop"}],
        "stop_reason": "tool_use",
    })
    normalized = message_to_normalized_invocation(request, response)
    assert normalized.output.message is not None
    tool_block = normalized.output.message.content[0]
    assert tool_block.kind == "tool_use"
    assert tool_block.tool_input == {}  # not None, not missing


def test_anthropic_stream_malformed_input_json_yields_empty_dict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed input_json_delta reassembly: logs a WARNING (level +
    logger + substring, not exact wording — so copy edits don't break
    the test) and yields tool_input={}."""
    import logging

    from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
        stream_to_normalized_invocation,
    )
    from slashid_ai_forwarder_core.normalize.anthropic.schema import (
        AnthropicRequestBody,
        AnthropicStreamEvent,
    )

    request = AnthropicRequestBody.model_validate({
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    })
    stream = TypeAdapter(list[AnthropicStreamEvent]).validate_python([
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
    ])
    with caplog.at_level(
        logging.WARNING,
        logger="slashid_ai_forwarder_core.normalize.anthropic.normalize",
    ):
        normalized = stream_to_normalized_invocation(request, stream)

    assert normalized.output.message is not None
    block = normalized.output.message.content[0]
    assert block.kind == "tool_use"
    assert block.tool_input == {}

    # Level + logger + substring — no exact-wording match.
    matches = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "slashid_ai_forwarder_core.normalize.anthropic.normalize"
        and "malformed" in r.message
    ]
    assert len(matches) == 1


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
