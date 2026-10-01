"""Rounds, and the hashes that let a consumer stitch events into a conversation.

A round is the messages the model consumed and the response it gave. Its hash
covers a projection that survives replay, so the response in event k and the
same message replayed in event k+1's request hash alike.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

# Annotations only: ``normalized.types`` imports ``events`` at load, and
# ``events`` imports this module, so a runtime import here would cycle.
if TYPE_CHECKING:
    from .normalize.normalized.types import NormalizedContent, NormalizedMessage

# Not digests, so neither can collide with a real hash.
START = "start"  # the list reaches the conversation's first round
TRUNCATED = "..."  # older rounds exist beyond the list


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "tool_use", "tool_result", "attachment", "compaction"]
    text: str | None = None
    digest: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: JsonValue = None
    tool_output: JsonValue = None
    tool_is_error: bool | None = None


class _Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: list[_Block]


@dataclass(frozen=True)
class Round:
    consumed: list[NormalizedMessage]
    answer: list[NormalizedMessage]

    def digest(self) -> str | None:
        """sha256 of the projected round; ``None`` when there is no answer to hash."""
        projected_answer = project(self.answer)
        if not projected_answer:
            return None
        # An answer is assistant-only and ``consumed`` never is, so nothing merges across them.
        projected = [*project(self.consumed), *projected_answer]
        body = [m.model_dump(mode="json", exclude_none=True) for m in projected]
        serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(serialized).hexdigest()


def _project_block(block: NormalizedContent) -> _Block | None:
    match block.kind:
        case "text":
            return _Block(kind="text", text=block.text)
        case "tool_use":
            return _Block(
                kind="tool_use",
                tool_use_id=block.tool_use_id,
                tool_name=block.tool_name,
                tool_input=block.tool_input,
            )
        case "tool_result":
            return _Block(
                kind="tool_result",
                tool_use_id=block.tool_use_id,
                tool_output=block.tool_output,
                tool_is_error=block.tool_is_error,
            )
        case "image" | "audio" | "document":
            return _Block(kind="attachment")
        case "compaction":
            return _Block(kind="compaction", digest=block.text)
        case _:
            return None


def project(messages: Sequence[NormalizedMessage]) -> list[_Message]:
    """The part of ``messages`` that is the same as a response and as replayed history.

    Adjacent messages of one role merge: a run can be one message in the event
    and several in the next request.
    """
    out: list[_Message] = []
    for message in messages:
        if message.role == "system":
            continue
        projected = [_project_block(content) for content in message.content]
        blocks = [b for b in projected if b is not None]
        if not blocks:
            continue
        if out and out[-1].role == message.role:
            out[-1].content.extend(blocks)
        else:
            out.append(_Message(role=message.role, content=blocks))
    return out


def _is_compaction(message: NormalizedMessage) -> bool:
    return message.role == "assistant" and any(c.kind == "compaction" for c in message.content)


def _rounds(messages: Sequence[NormalizedMessage]) -> Iterator[Round]:
    """Rounds of ``messages``, newest first. The first has no answer when the
    transcript ends on a message that is not the model's. A compaction is a
    response of its own, never merged with the assistant run before it."""
    end = len(messages)
    while end:
        answer_end = end
        if _is_compaction(messages[end - 1]):
            end -= 1
        else:
            while (
                end
                and messages[end - 1].role == "assistant"
                and not _is_compaction(messages[end - 1])
            ):
                end -= 1
        consumed_end = end
        while end and messages[end - 1].role != "assistant":
            end -= 1
        yield Round(list(messages[end:consumed_end]), list(messages[consumed_end:answer_end]))


def round_links(
    messages: Sequence[NormalizedMessage], *, depth: int, truncated: bool = False
) -> tuple[str | None, list[str]]:
    """``(round_hash, recent_round_hashes)`` for an event.

    ``messages`` is the transcript including the event's response, if it has
    one. The list is newest first, at most ``depth`` hashes, and ends with
    ``START`` when it reaches round one and ``TRUNCATED`` when it does not.
    ``truncated`` ends the list with ``TRUNCATED`` even when it reaches round
    one (a history known to be incomplete). Only the last ``depth`` rounds are
    projected.
    """
    own: str | None = None
    hashes: list[str] = []
    reaches_start = True
    for index, round_ in enumerate(_rounds(messages)):
        digest = round_.digest()
        if index == 0:
            own = digest
        if digest is None:
            continue
        if len(hashes) == depth:
            reaches_start = False
            break
        hashes.append(digest)
    if not hashes:
        return None, []
    hashes.append(START if reaches_start and not truncated else TRUNCATED)
    return own, hashes
