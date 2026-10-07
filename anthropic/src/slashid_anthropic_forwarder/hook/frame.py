"""Wire model for the Inference hooks prompt frame, and the transcript split.

The envelope tolerates unknown fields and unknown discriminator values:
the protocol grows by addition, and rejecting a delivery over something
new is a webhook failure. ``messages`` reuse the shared Anthropic schema
— a hook transcript is the Messages API content model plus attachments.
That schema pins ``role`` to user/assistant, so a frame carrying a new
role fails to parse and ``main.py``'s guard answers allow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class Actor(_Lenient):
    # Union discriminated on type; "user" is the only value sent today.
    # Both id and email_address are documented nullable.
    type: str
    id: str | None = None
    email_address: str | None = None


class Source(_Lenient):
    # Open string: claude-ai, claude-code, cowork, config-test, and values
    # not yet invented. Advisory routing metadata, not a trust boundary.
    application: str | None = None


class Frame(_Lenient):
    """What every hook delivery carries, whichever event it is."""

    type: str
    request_id: str
    tenant_id: str | None = None
    actor: Actor = Field(default_factory=lambda: Actor(type="unknown"))
    source: Source = Field(default_factory=Source)
    session_id: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_connection_test(self) -> bool:
        """Anthropic's synthetic probe: the Test connection button and the
        circuit breaker's recovery checks. Carries no user content, so it
        bypasses the checks and writes no record."""
        return self.source.application == "config-test"


class PromptFrame(Frame):
    messages: list[AnthropicRequestMessage] = Field(default_factory=list)


class ToolInfo(_Lenient):
    """Who provides a tool. Only ``type`` is reliable: new kinds appear."""

    type: str = "client"
    toolset_name: str | None = None


class ToolCallBlock(_Lenient):
    type: str
    id: str | None = None
    tool_name: str | None = None
    tool_info: ToolInfo = Field(default_factory=ToolInfo)


class ToolUse(_Lenient):
    """A ``tool_use`` block that names its call and its tool."""

    id: str
    tool_name: str
    tool_info: ToolInfo


class ToolCallMessage(_Lenient):
    role: str
    content: list[ToolCallBlock] | str = ""


class ToolCallFrame(Frame):
    """One verdict covers every call in the response: ``messages`` holds only
    the assistant message that asked for them, text blocks and one
    ``tool_use`` per call."""

    messages: list[ToolCallMessage] = Field(default_factory=list)

    def tool_uses(self) -> list[ToolUse]:
        """The calls of the last message; the protocol may add earlier ones."""
        if not self.messages or isinstance(self.messages[-1].content, str):
            return []
        return [
            ToolUse(id=block.id, tool_name=block.tool_name, tool_info=block.tool_info)
            for block in self.messages[-1].content
            if block.type == "tool_use" and block.id and block.tool_name
        ]


@dataclass(frozen=True)
class Split:
    """``[… before …][ trailing assistant run ][ fresh ]``.

    ``fresh`` is everything after the last assistant message: what the
    model is about to read, and what the verdict scans. It is the tail
    record's content and it contributes nothing to the record this frame
    emits — attribution runs one round behind. It can span several
    messages — deferred-tool loading appends a user message after a
    tool_result, and an enforced denial leaves two user runs in a row.

    ``assistant_run`` is the last run of consecutive assistant messages,
    which is one response however many messages it arrived as. It is the
    previous invocation's answer — the run the emitted record names, and
    the record's ``output`` — and it is empty on a first turn, which is a
    frame with no previous invocation to report at all.

    ``before`` is everything ahead of that run: the transcript the
    emitted record's ``input`` ends with. Its own last round is the one
    that run consumed, which is why handing it to the shared helpers
    attributes ``used_tools`` and ``accessed_files`` correctly.
    """

    before: list[AnthropicRequestMessage]
    assistant_run: list[AnthropicRequestMessage]
    fresh: list[AnthropicRequestMessage]


def split_transcript(frame: PromptFrame) -> Split:
    """Partition a frame's transcript. The scan is ``after_last_assistant``
    rather than a local copy: the same rule decides attribution inside
    ``extract_tool_result_files`` and ``events.py::used_tools_of``, and a
    second implementation that drifted would re-attribute files."""
    messages = frame.messages
    fresh = list(after_last_assistant(messages))
    head = messages[: len(messages) - len(fresh)]
    start = len(head)
    while start > 0 and head[start - 1].role == "assistant":
        start -= 1
    return Split(before=head[:start], assistant_run=head[start:], fresh=fresh)
