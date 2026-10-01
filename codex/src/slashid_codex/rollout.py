"""Codex rollout lines (``~/.codex/sessions/…/rollout-*.jsonl``), one model per
line type the cursor folds. Other types parse to ``None``."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, Field, ValidationError
from slashid_ai_forwarder_core.normalize._base import _LenientModel
from slashid_ai_forwarder_core.normalize.openai.responses.schema import ResponsesItem

from .usage import CodexUsage


class RolloutLineError(ValueError):
    """Invalid JSON, or a modelled line type that fails validation."""


class _Frozen(_LenientModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --------------------------------------------------------------------------
# Payloads
# --------------------------------------------------------------------------


class BaseInstructions(_Frozen):
    text: str | None = None


def _instructions(value: object) -> object:
    return {"text": value} if isinstance(value, str) else value


class HistoryBase(_Frozen):
    thread_id: str
    end_byte_offset: int


class SessionMeta(_Frozen):
    id: str
    session_id: str | None = None
    originator: str | None = None
    cli_version: str | None = None
    # ``{provenance, text}``; a plain string is accepted too.
    base_instructions: Annotated[BaseInstructions | None, BeforeValidator(_instructions)] = None
    forked_from_id: str | None = None
    history_base: HistoryBase | None = None


class TurnContext(_Frozen):
    turn_id: str
    model: str | None = None
    cwd: str | None = None


class TokenUsageRecord(_Frozen):
    response_id: str
    turn_id: str
    usage: CodexUsage


class Compacted(_Frozen):
    compaction_response_id: str | None = None
    window_number: int | None = None
    replacement_history: list[ResponsesItem] = Field(default_factory=list)


class ParsedCmd(_Frozen):
    # ``read``, ``list_files``, ``unknown``, …
    type: str
    cmd: str | None = None
    name: str | None = None
    path: str | None = None


class CommandExecution(_Frozen):
    type: Literal["CommandExecution"]
    # ``call_…`` in function mode, ``exec-<uuid>`` in script mode.
    id: str
    command: list[str] = Field(default_factory=list)
    # A ``file://`` URL.
    cwd: str | None = None
    parsed_cmd: list[ParsedCmd] = Field(default_factory=list)
    exit_code: int | None = None
    status: str | None = None


class ImageView(_Frozen):
    type: Literal["ImageView"]
    id: str
    # A percent-encoded ``file://`` URL.
    path: str


class UserMessagePart(_Frozen):
    # ``text`` or ``local_image``.
    type: str
    text: str | None = None
    path: str | None = None


class UserMessageItem(_Frozen):
    type: Literal["UserMessage"]
    id: str
    content: list[UserMessagePart] = Field(default_factory=list)


class OtherItem(_Frozen):
    type: str
    id: str | None = None


CodexItem = Annotated[
    CommandExecution | ImageView | UserMessageItem | OtherItem,
    Field(union_mode="left_to_right"),
]


class ItemCompleted(_Frozen):
    type: Literal["item_completed"] = "item_completed"
    turn_id: str | None = None
    item: CodexItem


class TurnAborted(_Frozen):
    type: Literal["turn_aborted"] = "turn_aborted"
    turn_id: str | None = None
    reason: str | None = None


class TaskStarted(_Frozen):
    type: Literal["task_started"] = "task_started"
    turn_id: str | None = None


class TaskComplete(_Frozen):
    type: Literal["task_complete"] = "task_complete"
    turn_id: str | None = None


EventMsg = ItemCompleted | TurnAborted | TaskStarted | TaskComplete

Payload = SessionMeta | TurnContext | ResponsesItem | TokenUsageRecord | Compacted | EventMsg


class RolloutLine(_Frozen):
    # When the line was written.
    timestamp: datetime
    type: str
    payload: Payload


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class _PayloadHeader(_LenientModel):
    type: str | None = None


class _Header(_LenientModel):
    type: str
    payload: _PayloadHeader | None = None


class _SessionMetaLine(_LenientModel):
    timestamp: datetime
    payload: SessionMeta


class _TurnContextLine(_LenientModel):
    timestamp: datetime
    payload: TurnContext


class _ResponseItemLine(_LenientModel):
    timestamp: datetime
    payload: ResponsesItem


class _TokenUsageLine(_LenientModel):
    timestamp: datetime
    payload: TokenUsageRecord


class _CompactedLine(_LenientModel):
    timestamp: datetime
    payload: Compacted


class _EventMsgLine(_LenientModel):
    timestamp: datetime
    payload: Annotated[EventMsg, Field(discriminator="type")]


_Envelope = (
    _SessionMetaLine
    | _TurnContextLine
    | _ResponseItemLine
    | _TokenUsageLine
    | _CompactedLine
    | _EventMsgLine
)

_LINES: dict[str, type[_Envelope]] = {
    "session_meta": _SessionMetaLine,
    "turn_context": _TurnContextLine,
    "response_item": _ResponseItemLine,
    "token_usage_record": _TokenUsageLine,
    "compacted": _CompactedLine,
}
_EVENTS = frozenset({"item_completed", "turn_aborted", "task_started", "task_complete"})


def parse_line(raw: bytes) -> RolloutLine | None:
    """``None`` for line and event types the cursor does not fold."""
    try:
        header = _Header.model_validate_json(raw)
    except ValidationError as exc:
        raise RolloutLineError(str(exc)) from exc
    if header.type == "event_msg":
        if header.payload is None or header.payload.type not in _EVENTS:
            return None
        envelope: type[_Envelope] = _EventMsgLine
    elif header.type in _LINES:
        envelope = _LINES[header.type]
    else:
        return None
    try:
        line = envelope.model_validate_json(raw)
    except ValidationError as exc:
        raise RolloutLineError(str(exc)) from exc
    return RolloutLine.model_construct(
        timestamp=line.timestamp, type=header.type, payload=line.payload
    )
