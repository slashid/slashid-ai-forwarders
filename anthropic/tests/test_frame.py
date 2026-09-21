"""Prompt-frame envelope and the transcript split every path relies on."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicRequestMessage,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
)
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.hook.frame import PromptFrame, Source, split_transcript

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> PromptFrame:
    return PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))


def test_tool_result_frame_parses_with_the_shared_blocks() -> None:
    frame = load("frame_tool_result")
    assert frame.type == "prompt"
    assert frame.actor.id == "user_01AbCdEfGhIjKlMnOpQrStUv"
    assert frame.source.application == "claude-code"
    assert frame.session_id == "00000002-0000-4000-8000-000000000000"
    # The hook spells it `tool_name`; the alias from Chunk 2 Task 2.1 is why
    # this reads as `.name`.
    use = frame.messages[1].content[1]
    assert isinstance(use, AnthropicToolUseBlock) and use.name == "Read"
    result = frame.messages[2].content[0]
    assert isinstance(result, AnthropicToolResultBlock)
    assert result.tool_use_id == use.id and result.is_error is False


def test_three_attachments_sit_between_two_text_blocks() -> None:
    """frame_attachment.json: one user message, five blocks, and nullable
    metadata on the attachments — the JPEG and the PDF have no file_name,
    the text upload and the PDF no size_bytes."""
    blocks = load("frame_attachment").messages[0].content
    assert [type(b).__name__ for b in blocks] == [
        "AnthropicTextBlock",
        "AnthropicAttachmentBlock",
        "AnthropicAttachmentBlock",
        "AnthropicAttachmentBlock",
        "AnthropicTextBlock",
    ]
    txt, jpeg, pdf = (b for b in blocks if isinstance(b, AnthropicAttachmentBlock))
    assert (txt.file_name, txt.size_bytes) == ("maria.txt", None)
    assert (jpeg.file_name, jpeg.size_bytes, jpeg.text) == (None, 70657, None)
    assert pdf.file_name is None and pdf.size_bytes is None and pdf.text is not None


def test_mcp_frame_keeps_its_two_consecutive_user_messages() -> None:
    frame = load("frame_mcp_tool")
    assert [m.role for m in frame.messages] == [
        "user",
        "assistant",
        "user",
        "user",
        "assistant",
        "user",
    ]
    use = frame.messages[4].content[0]
    assert isinstance(use, AnthropicToolUseBlock) and use.name == "mcp__demo__echo"
    # A server-executed tool's result is a placeholder, not content.
    placeholder = frame.messages[2].content[0]
    assert isinstance(placeholder, AnthropicToolResultBlock)
    assert placeholder.content == "[non-text content]"


def test_failed_tool_result_carries_is_error() -> None:
    result = load("frame_tool_error").messages[2].content[0]
    assert isinstance(result, AnthropicToolResultBlock) and result.is_error is True


def test_forward_compatible_shapes_parse() -> None:
    frame = PromptFrame.model_validate(
        {
            "type": "response",
            "request_id": "r",
            "tenant_id": None,
            "actor": {"type": "robot", "id": None, "email_address": None},
            "source": {"application": "brand-new-surface"},
            "messages": [{"role": "user", "content": [{"type": "sparkle", "glitter": 1}]}],
            "metadata": {"unexpected": "key"},
            "brand_new_top_level": True,
        }
    )
    assert frame.type == "response"
    assert frame.actor.type == "robot" and frame.actor.id is None
    assert frame.session_id is None and frame.model is None
    assert frame.messages[0].content[0].type == "sparkle"


def test_connection_test_is_recognised() -> None:
    frame = load("frame_first_turn")
    assert not frame.is_connection_test()
    probe = frame.model_copy(update={"source": Source(application="config-test")})
    assert probe.is_connection_test()


@yaml_pytest(filename="test_split_transcript.yaml")
def test_split_transcript(
    fixture: str,
    take: int | None,
    append: list[dict[str, Any]],
    before: int,
    assistant_run: int,
    fresh: int,
) -> None:
    frame = load(fixture)
    messages = list(frame.messages)[:take] + [
        AnthropicRequestMessage.model_validate(m) for m in append
    ]
    split = split_transcript(frame.model_copy(update={"messages": messages}))
    assert (len(split.before), len(split.assistant_run), len(split.fresh)) == (
        before,
        assistant_run,
        fresh,
    )
    # The three parts partition the transcript — nothing dropped, nothing reordered.
    assert split.before + split.assistant_run + split.fresh == messages
