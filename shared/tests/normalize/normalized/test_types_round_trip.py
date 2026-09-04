"""Round-trip validation for the canonical NormalizedInvocation types.

Not testing translates — those are per-vendor. This file's job is to
verify the pydantic models themselves: field defaults, Literal
constraints, sub-model composition, model_dump/model_validate round-trip.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)


def test_normalized_content_minimal() -> None:
    """Only `kind` is required. All other fields default to None / False."""
    c = NormalizedContent(kind="text")
    assert c.kind == "text"
    assert c.text is None
    assert c.tool_input is None
    assert c.tool_is_error is False
    assert c.tool_executor is None


def test_normalized_content_kind_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        NormalizedContent(kind="wat")  # ty: ignore[invalid-argument-type]


def test_normalized_content_extras_ignored() -> None:
    """_LenientModel: unknown fields dropped silently (safe against vendor evolution)."""
    c = NormalizedContent.model_validate({"kind": "text", "text": "hi", "extra_key": 42})
    assert c.text == "hi"
    assert not hasattr(c, "extra_key")


def test_normalized_message_role_literal() -> None:
    """`role` accepts only system/user/assistant/tool."""
    NormalizedMessage(role="system", content=[])
    NormalizedMessage(role="user", content=[])
    NormalizedMessage(role="assistant", content=[])
    NormalizedMessage(role="tool", content=[])
    with pytest.raises(ValidationError):
        NormalizedMessage(role="developer", content=[])  # ty: ignore[invalid-argument-type]


def test_normalized_invocation_defaults() -> None:
    """Empty invocation is valid — used for `parsed_as="unknown"` fallthrough."""
    n = NormalizedInvocation()
    assert n.tokens.input == 0
    assert n.tokens.output == 0
    assert n.input == NormalizedInvocationInput()
    assert n.output == NormalizedInvocationOutput()
    assert n.output.stop_reason == "unknown"


def test_normalized_invocation_round_trip_json() -> None:
    """model_dump(mode='json') → model_validate is lossless for the common shape."""
    n = NormalizedInvocation(
        input=NormalizedInvocationInput(
            messages=[
                NormalizedMessage(
                    role="system",
                    content=[NormalizedContent(kind="text", text="You are helpful")],
                ),
                NormalizedMessage(
                    role="user",
                    content=[NormalizedContent(kind="text", text="hi")],
                ),
            ],
        ),
        output=NormalizedInvocationOutput(
            message=NormalizedMessage(
                role="assistant",
                content=[NormalizedContent(kind="text", text="hello")],
            ),
            stop_reason="end_turn",
        ),
    )
    dumped = n.model_dump(mode="json", exclude_none=True)
    reparsed = NormalizedInvocation.model_validate(dumped)
    assert reparsed == n


def test_normalized_content_tool_use_shape() -> None:
    """tool_use block: id + name + input + executor together."""
    c = NormalizedContent(
        kind="tool_use",
        tool_use_id="toolu_1",
        tool_name="read_file",
        tool_input={"path": "/etc/hostname"},
        tool_executor="client",
    )
    dumped = c.model_dump(mode="json", exclude_none=True)
    assert dumped == {
        "kind": "tool_use",
        "tool_use_id": "toolu_1",
        "tool_name": "read_file",
        "tool_input": {"path": "/etc/hostname"},
        "tool_is_error": False,
        "tool_executor": "client",
    }


def test_normalized_content_byte_length_non_negative() -> None:
    """byte_length uses pydantic NonNegativeInt — 0 valid, negative rejected."""
    NormalizedContent(kind="text", byte_length=0)
    NormalizedContent(kind="text", byte_length=42)
    with pytest.raises(ValidationError):
        NormalizedContent(kind="text", byte_length=-1)
