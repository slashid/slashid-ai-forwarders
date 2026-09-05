"""Converse response wire-schema round-trips and key-tagged discriminator."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseReasoningBlock,
    ConverseResponse,
    ConverseTextBlock,
    ConverseTool,
    ConverseToolConfig,
    ConverseToolInputSchema,
    ConverseToolSpec,
    ConverseToolUseBlock,
    ConverseUnknownBlock,
)

# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


def test_converse_response_round_trip_text() -> None:
    raw = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "hello"}],
            },
        },
        "stopReason": "end_turn",
    }
    resp = ConverseResponse.model_validate(raw)
    assert isinstance(resp.output.message.content[0], ConverseTextBlock)
    assert resp.output.message.content[0].text == "hello"
    assert resp.stopReason == "end_turn"
    assert resp.model_dump(exclude_none=True) == raw


def test_converse_response_tool_use_block() -> None:
    raw = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tooluse_x",
                            "name": "read",
                            "input": {"path": "/x"},
                        },
                    },
                ],
            },
        },
    }
    resp = ConverseResponse.model_validate(raw)
    block = resp.output.message.content[0]
    assert isinstance(block, ConverseToolUseBlock)
    assert block.toolUse.toolUseId == "tooluse_x"
    assert block.toolUse.name == "read"
    assert block.toolUse.input == {"path": "/x"}
    assert resp.model_dump(exclude_none=True) == raw


def test_converse_response_reasoning_block() -> None:
    raw = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "reasoningContent": {
                            "reasoningText": {"text": "reasoning...", "signature": "sig"},
                        },
                    },
                ],
            },
        },
    }
    resp = ConverseResponse.model_validate(raw)
    block = resp.output.message.content[0]
    assert isinstance(block, ConverseReasoningBlock)
    assert block.reasoningContent.reasoningText is not None
    assert block.reasoningContent.reasoningText.text == "reasoning..."


def test_converse_response_unknown_block_key_falls_through_to_catchall() -> None:
    # Bedrock has other block keys (image, document, video, cachePoint,
    # guardContent, toolResult). ConverseUnknownBlock catches them all.
    raw = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"image": {"format": "png", "source": {"bytes": "..."}}}],
            },
        },
    }
    resp = ConverseResponse.model_validate(raw)
    block = resp.output.message.content[0]
    assert isinstance(block, ConverseUnknownBlock)


def test_converse_response_empty_content() -> None:
    raw = {"output": {"message": {"role": "assistant", "content": []}}}
    resp = ConverseResponse.model_validate(raw)
    assert resp.output.message.content == []


def test_converse_response_extra_top_level_field_ignored() -> None:
    raw = {
        "output": {"message": {"role": "assistant", "content": []}},
        "additionalModelResponseFields": {"foo": "bar"},
    }
    resp = ConverseResponse.model_validate(raw)
    assert "additionalModelResponseFields" not in resp.model_dump(exclude_none=True)


def test_converse_response_wrong_role_rejected() -> None:
    raw = {"output": {"message": {"role": "user", "content": []}}}
    with pytest.raises(ValidationError):
        ConverseResponse.model_validate(raw)


def test_converse_response_missing_output_rejected() -> None:
    with pytest.raises(ValidationError):
        ConverseResponse.model_validate({"stopReason": "end_turn"})


# --------------------------------------------------------------------------
# Tool config
# --------------------------------------------------------------------------


def test_converse_tool_config_round_trip() -> None:
    raw = {
        "tools": [
            {
                "toolSpec": {
                    "name": "read",
                    "description": "Read a file.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                },
            },
        ],
    }
    cfg = ConverseToolConfig.model_validate(raw)
    assert cfg.tools[0].toolSpec.name == "read"
    input_schema = cfg.tools[0].toolSpec.inputSchema
    assert input_schema is not None
    assert input_schema.json_ == raw["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert cfg.model_dump(by_alias=True, exclude_none=True) == raw


def test_converse_tool_input_schema_accepts_populate_by_name_and_alias() -> None:
    # by-alias uses "json"; by-name uses "json_" (Python-reserved-word workaround).
    s1 = ConverseToolInputSchema.model_validate({"json": {"type": "object"}})
    s2 = ConverseToolInputSchema.model_validate({"json_": {"type": "object"}})
    assert s1.json_ == {"type": "object"}
    assert s2.json_ == {"type": "object"}


def test_converse_tool_config_empty_list() -> None:
    cfg = ConverseToolConfig.model_validate({"tools": []})
    assert cfg.tools == []


# Silence unused-import warning while keeping the symbol available for
# future test authors who might reach for it.
_ = ConverseTool
_ = ConverseToolSpec


# ==========================================================================
# Request-side schemas (Phase 2, Chunk 4)
# ==========================================================================


def test_converse_request_body_minimal() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import ConverseRequestBody

    body = ConverseRequestBody.model_validate(
        {
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        }
    )
    assert body.messages[0].role == "user"
    assert body.system is None
    assert body.toolConfig is None


def test_converse_request_body_system_list_form() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import ConverseRequestBody

    body = ConverseRequestBody.model_validate(
        {
            "system": [{"text": "You are helpful."}],
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
        }
    )
    assert body.system is not None
    assert body.system[0].text == "You are helpful."


def test_converse_request_body_ignores_non_content_settings() -> None:
    """inferenceConfig, additionalModelRequestFields, guardrailConfig — all dropped."""
    from slashid_ai_forwarder_core.normalize.converse.schema import ConverseRequestBody

    body = ConverseRequestBody.model_validate(
        {
            "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0.7},
            "additionalModelRequestFields": {"top_k": 40},
            "guardrailConfig": {"guardrailIdentifier": "g_1"},
        }
    )
    assert not hasattr(body, "inferenceConfig")
    assert not hasattr(body, "additionalModelRequestFields")


def test_converse_request_message_role_widened() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import ConverseRequestBody

    body = ConverseRequestBody.model_validate(
        {
            "messages": [
                {"role": "user", "content": [{"text": "hi"}]},
                {"role": "assistant", "content": [{"text": "hello"}]},
                {"role": "user", "content": [{"text": "how are you"}]},
            ],
        }
    )
    assert [m.role for m in body.messages] == ["user", "assistant", "user"]


def test_converse_tool_result_block_in_user_message() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import (
        ConverseRequestBody,
        ConverseToolResultBlock,
    )

    body = ConverseRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": "tooluse_1",
                                "content": [{"text": "the answer is 42"}],
                                "status": "success",
                            },
                        },
                    ],
                },
            ],
        }
    )
    block = body.messages[0].content[0]
    assert isinstance(block, ConverseToolResultBlock)
    assert block.toolResult.toolUseId == "tooluse_1"


def test_converse_document_block_inline_bytes() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import (
        ConverseDocumentBlock,
        ConverseRequestBody,
    )

    body = ConverseRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "document": {
                                "name": "notes.txt",
                                "format": "txt",
                                "source": {"bytes": "aGVsbG8="},
                            },
                        },
                    ],
                },
            ],
        }
    )
    block = body.messages[0].content[0]
    assert isinstance(block, ConverseDocumentBlock)
    assert block.document.name == "notes.txt"
    assert block.document.format == "txt"
    assert block.document.source.bytes == "aGVsbG8="
    assert block.document.source.s3Location is None


def test_converse_document_block_s3_source() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import (
        ConverseDocumentBlock,
        ConverseRequestBody,
    )

    body = ConverseRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "document": {
                                "name": "report.pdf",
                                "format": "pdf",
                                "source": {
                                    "s3Location": {"uri": "s3://my-bucket/report.pdf"},
                                },
                            },
                        },
                    ],
                },
            ],
        }
    )
    block = body.messages[0].content[0]
    assert isinstance(block, ConverseDocumentBlock)
    assert block.document.source.bytes is None
    assert block.document.source.s3Location is not None
    assert block.document.source.s3Location.uri == "s3://my-bucket/report.pdf"


def test_converse_image_block_inline_bytes() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import (
        ConverseImageBlock,
        ConverseRequestBody,
    )

    body = ConverseRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "image": {
                                "format": "png",
                                "source": {"bytes": "iVBORw0KGgo="},
                            },
                        },
                    ],
                },
            ],
        }
    )
    block = body.messages[0].content[0]
    assert isinstance(block, ConverseImageBlock)
    assert block.image.format == "png"
    assert block.image.source.bytes == "iVBORw0KGgo="


def test_converse_image_block_s3_source() -> None:
    from slashid_ai_forwarder_core.normalize.converse.schema import (
        ConverseImageBlock,
        ConverseRequestBody,
    )

    body = ConverseRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "image": {
                                "format": "jpeg",
                                "source": {
                                    "s3Location": {"uri": "s3://my-bucket/photo.jpg"},
                                },
                            },
                        },
                    ],
                },
            ],
        }
    )
    block = body.messages[0].content[0]
    assert isinstance(block, ConverseImageBlock)
    assert block.image.source.bytes is None
    assert block.image.source.s3Location is not None
    assert block.image.source.s3Location.uri == "s3://my-bucket/photo.jpg"


def test_converse_s3_location_ignores_extra_fields() -> None:
    """s3Location commonly carries owner-account etc. — those get dropped by
    the _LenientModel base."""
    from slashid_ai_forwarder_core.normalize.converse.schema import ConverseS3Location

    loc = ConverseS3Location.model_validate({"uri": "s3://b/k", "bucketOwner": "123456789012"})
    assert loc.uri == "s3://b/k"
    assert loc.model_dump(exclude_none=True) == {"uri": "s3://b/k"}


def test_converse_unknown_block_still_catches_video() -> None:
    """After adding document/image to the union, other unknown keys still
    fall through to ConverseUnknownBlock."""
    from slashid_ai_forwarder_core.normalize.converse.schema import (
        ConverseRequestBody,
        ConverseUnknownBlock,
    )

    body = ConverseRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"video": {"format": "mp4", "source": {"bytes": "..."}}}],
                },
            ],
        }
    )
    block = body.messages[0].content[0]
    assert isinstance(block, ConverseUnknownBlock)
