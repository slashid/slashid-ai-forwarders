"""Pure-function transforms: Anthropic-side helpers used by mil_normalize."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    extract_stream_usage,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicStreamEvent,
    AnthropicUsage,
)
from slashid_ai_forwarder_core.testing import yaml_pytest


def _config() -> BaseConfig:
    return BaseConfig(endpoint="http://test", push_token="test")


# --------------------------------------------------------------------------
# Behaviour invariants for the Anthropic → NormalizedInvocation translates
# --------------------------------------------------------------------------


async def test_anthropic_message_tool_use_with_null_input_becomes_empty_dict() -> None:
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

    request = AnthropicRequestBody.model_validate(
        {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "noop"}]}],
        }
    )
    response = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_x", "name": "noop"}],
            "stop_reason": "tool_use",
        }
    )
    normalized = await message_to_normalized_invocation(request, response, config=_config())
    assert normalized.output.message is not None
    tool_block = normalized.output.message.content[0]
    assert tool_block.kind == "tool_use"
    assert tool_block.tool_input == {}  # not None, not missing


async def test_anthropic_stream_malformed_input_json_yields_empty_dict(
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

    request = AnthropicRequestBody.model_validate(
        {
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
    )
    stream = TypeAdapter(list[AnthropicStreamEvent]).validate_python(
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
        normalized = await stream_to_normalized_invocation(request, stream, config=_config())

    assert normalized.output.message is not None
    block = normalized.output.message.content[0]
    assert block.kind == "tool_use"
    assert block.tool_input == {}

    # Level + logger + substring — no exact-wording match.
    matches = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "slashid_ai_forwarder_core.normalize.anthropic.normalize"
        and "malformed" in r.message
    ]
    assert len(matches) == 1


# --------------------------------------------------------------------------
# extract_stream_usage
# --------------------------------------------------------------------------


@yaml_pytest()
def test_extract_stream_usage(
    body: list[AnthropicStreamEvent],
    expected: AnthropicUsage,
) -> None:
    assert extract_stream_usage(body) == expected


async def test_hook_spelled_tool_use_survives_request_translation() -> None:
    """Regression: a hook-spelled tool_use used to validate as
    AnthropicUnknownBlock and get dropped silently by
    ``_translate_request_content`` — every hook tool call went missing."""
    from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
        message_to_normalized_invocation,
    )
    from slashid_ai_forwarder_core.normalize.anthropic.schema import (
        AnthropicMessage,
        AnthropicRequestBody,
    )

    request = AnthropicRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01Dqhr",
                            "tool_name": "Read",
                            "input": {"file_path": "/home/alice/proj/notes.txt"},
                        }
                    ],
                }
            ]
        }
    )
    response = AnthropicMessage.model_validate(
        {"type": "message", "role": "assistant", "content": [], "stop_reason": "end_turn"}
    )
    normalized = await message_to_normalized_invocation(request, response, config=_config())
    blocks = normalized.input.messages[0].content
    assert [b.kind for b in blocks] == ["tool_use"]
    assert blocks[0].tool_name == "Read"
    assert blocks[0].tool_use_id == "toolu_01Dqhr"
    assert blocks[0].tool_input == {"file_path": "/home/alice/proj/notes.txt"}


async def test_attachment_blocks_become_documents_in_frame_order() -> None:
    """Shape and ordering from anthropic/tests/fixtures/frame_attachment.json:
    three attachments between two text blocks. ``byte_length`` is the length
    of the extracted text — the bytes the receiver hashes — not the frame's
    ``size_bytes``, which is null for text uploads anyway."""
    from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
        message_to_normalized_invocation,
    )
    from slashid_ai_forwarder_core.normalize.anthropic.schema import (
        AnthropicMessage,
        AnthropicRequestBody,
    )

    request = AnthropicRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "<uploaded_files>\n</uploaded_files>\n\n"},
                        {
                            "type": "attachment",
                            "media_type": "text/plain",
                            "size_bytes": None,
                            "file_name": "maria.txt",
                            "text": "Maria tinha um carneirinho\n",
                        },
                        {
                            "type": "attachment",
                            "media_type": "image/jpeg",
                            "size_bytes": 70657,
                            "file_name": None,
                            "text": None,
                        },
                        {
                            "type": "attachment",
                            "media_type": "application/pdf",
                            "size_bytes": None,
                            "file_name": None,
                            "text": "Extracted document text\r\nline two\r\nline three\r\n",
                        },
                        {"type": "text", "text": "teste com anexos"},
                    ],
                }
            ]
        }
    )
    response = AnthropicMessage.model_validate(
        {"type": "message", "role": "assistant", "content": [], "stop_reason": "end_turn"}
    )
    normalized = await message_to_normalized_invocation(request, response, config=_config())
    blocks = normalized.input.messages[0].content
    assert [b.kind for b in blocks] == ["text", "document", "document", "document", "text"]
    assert blocks[1].text == "Maria tinha um carneirinho\n"
    assert blocks[1].byte_length == 27  # not size_bytes, which is null here
    assert blocks[1].media_type == "text/plain"
    # An image arrives with no name and no text: metadata only, nothing to hash.
    assert blocks[2].text is None
    assert blocks[2].byte_length is None
    assert blocks[2].media_type == "image/jpeg"
    # A PDF arrives as extracted text with CRLF line endings, preserved verbatim.
    assert blocks[3].byte_length == 47


async def test_attachment_with_unregistered_media_type_is_not_fatal() -> None:
    """A compliance listing carried ``"mime_type": "txt"``. The block must
    still translate, with media_type dropped rather than raising."""
    from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
        message_to_normalized_invocation,
    )
    from slashid_ai_forwarder_core.normalize.anthropic.schema import (
        AnthropicMessage,
        AnthropicRequestBody,
    )

    request = AnthropicRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "attachment", "media_type": "txt", "text": "x"}],
                }
            ]
        }
    )
    response = AnthropicMessage.model_validate(
        {"type": "message", "role": "assistant", "content": [], "stop_reason": "end_turn"}
    )
    normalized = await message_to_normalized_invocation(request, response, config=_config())
    block = normalized.input.messages[0].content[0]
    assert block.kind == "document"
    assert block.media_type is None
    assert block.byte_length == 1
