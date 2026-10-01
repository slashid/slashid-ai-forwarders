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


class PromptFrame(_Lenient):
    type: str
    request_id: str
    tenant_id: str | None = None
    actor: Actor = Field(default_factory=lambda: Actor(type="unknown"))
    source: Source = Field(default_factory=Source)
    messages: list[AnthropicRequestMessage] = Field(default_factory=list)
    session_id: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_connection_test(self) -> bool:
        """Anthropic's synthetic probe: the Test connection button and the
        circuit breaker's recovery checks. Carries no user content, so it
        bypasses the checks and writes no record."""
        return self.source.application == "config-test"


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
