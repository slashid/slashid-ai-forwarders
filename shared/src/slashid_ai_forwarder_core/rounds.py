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
from itertools import chain
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

# Annotations only: ``normalized.types`` imports ``events`` at load, and
# ``events`` imports this module, so a runtime import here would cycle.
if TYPE_CHECKING:
    from .normalize.normalized.types import NormalizedContent, NormalizedMessage

# Not a digest, so it cannot collide with a real hash.
CONVERSATION_START = "conversation-start"


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "tool_use", "tool_result", "attachment"]
    text: str | None = None
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


def _closed_rounds(messages: Sequence[NormalizedMessage], end: int) -> Iterator[Round]:
    """Rounds of ``messages[:end]`` (which ends on an assistant message), newest first."""
    while end:
        answer_end = end
        while end and messages[end - 1].role == "assistant":
            end -= 1
        consumed_end = end
        while end and messages[end - 1].role != "assistant":
            end -= 1
        yield Round(list(messages[end:consumed_end]), list(messages[consumed_end:answer_end]))


def round_links(
    history: Sequence[NormalizedMessage],
    answer: NormalizedMessage | None,
    *,
    depth: int,
) -> tuple[str | None, list[str]]:
    """``(round_hash, recent_round_hashes)`` for an event.

    ``history`` is the transcript before the response and ``answer`` the
    response, absent for a record with none. The list is newest first, at
    most ``depth`` hashes, and ends with the guard when it reaches round one.
    Only the last ``depth`` rounds are projected.
    """
    answered = answer is not None and bool(project([answer]))
    cut = len(history)
    while cut and history[cut - 1].role != "assistant":
        cut -= 1
    consumed = list(history[cut:])
    older = _closed_rounds(history, cut)
    newest: list[Round] = []
    if answer is not None and answered:
        if consumed or not history:
            newest.append(Round(consumed, [answer]))
        else:
            # The history ends on an assistant run, which the answer extends.
            last = next(older)
            newest.append(Round(last.consumed, [*last.answer, answer]))
    hashes: list[str] = []
    reaches_start = True
    for r in chain(newest, older):
        digest = r.digest()
        if digest is None:
            continue
        if len(hashes) == depth:
            reaches_start = False
            break
        hashes.append(digest)
    if not hashes:
        return None, []
    if reaches_start:
        hashes.append(CONVERSATION_START)
    return (hashes[0] if answered else None), hashes
