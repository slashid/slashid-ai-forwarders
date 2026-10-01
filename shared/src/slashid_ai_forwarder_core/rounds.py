"""Rounds, and the hashes that let a consumer stitch events into a conversation.

A round is the messages the model consumed and the response it gave. Its hash
covers a projection that survives replay, so the response in event k and the
same message replayed in event k+1's request hash alike.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
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
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant", "tool"]
    content: list[_Block]


@dataclass(frozen=True)
class Round:
    consumed: list[NormalizedMessage]
    answer: list[NormalizedMessage]


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
        blocks = [b for c in message.content if (b := _project_block(c)) is not None]
        if not blocks:
            continue
        if out and out[-1].role == message.role:
            out[-1] = _Message(role=message.role, content=[*out[-1].content, *blocks])
        else:
            out.append(_Message(role=message.role, content=blocks))
    return out


def completed_rounds(
    messages: Sequence[NormalizedMessage],
) -> tuple[list[Round], list[NormalizedMessage]]:
    """Rounds closed by an assistant run, and the messages after the last one."""
    rounds: list[Round] = []
    consumed: list[NormalizedMessage] = []
    answer: list[NormalizedMessage] = []
    for message in messages:
        if message.role == "assistant":
            answer.append(message)
            continue
        if answer:
            rounds.append(Round(consumed, answer))
            consumed, answer = [], []
        consumed.append(message)
    if answer:
        rounds.append(Round(consumed, answer))
        consumed = []
    return rounds, consumed


def round_hash(
    consumed: Sequence[NormalizedMessage], answer: Sequence[NormalizedMessage]
) -> str | None:
    """sha256 of the projected round; ``None`` when there is no answer to hash."""
    if not project(answer):
        return None
    body = [m.model_dump(mode="json", exclude_none=True) for m in project([*consumed, *answer])]
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


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
    """
    answered = answer is not None and bool(project([answer]))
    if not history and not answered:
        return None, []
    rounds, trailing = completed_rounds(history)
    rounds = [r for r in rounds if project(r.answer)]
    if answer is not None and answered:
        rounds.append(Round(trailing, [answer]))
    recent = [h for r in rounds[-depth:] if (h := round_hash(r.consumed, r.answer)) is not None]
    recent.reverse()
    if len(rounds) <= depth:
        recent.append(CONVERSATION_START)
    return (recent[0] if answered else None), recent
