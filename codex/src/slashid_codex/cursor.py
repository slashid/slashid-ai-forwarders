"""``RolloutCursor``: a position in a ``SessionLog`` and the history folded up
to it (spec "Applying lines")."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime

from pydantic import BaseModel, ConfigDict
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedMessage
from slashid_ai_forwarder_core.normalize.openai.responses.normalize import to_normalized
from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    Response,
    ResponsesCompaction,
    ResponsesCustomToolCall,
    ResponsesCustomToolCallOutput,
    ResponsesFunctionCall,
    ResponsesFunctionCallOutput,
    ResponsesInputTokensDetails,
    ResponsesItem,
    ResponsesMessage,
    ResponsesOutputTokensDetails,
    ResponsesReasoning,
    ResponsesRequest,
    ResponsesUnknownItem,
    ResponsesUsage,
    ResponsesWebSearchCall,
)
from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint

from .log import SessionLog
from .rollout import (
    CodexItem,
    Compacted,
    ItemCompleted,
    OtherItem,
    SessionMeta,
    TaskComplete,
    TaskStarted,
    TokenUsageRecord,
    TurnAborted,
    TurnContext,
    UserMessageItem,
)
from .shell_calls import SCRIPT_TOOL, map_custom_call, map_function_call
from .usage import CodexUsage

# ``item_completed`` types that are not tool executions.
_NOT_TOOL_ITEMS = frozenset({"AgentMessage", "Reasoning", "ContextCompaction"})


class RolloutInvocation(BaseModel):
    """One closed model response, with the history it consumed."""

    model_config = ConfigDict(frozen=True)

    # ``input`` is the whole history.
    request: ResponsesRequest
    response: Response
    response_id: str
    # The ``token_usage_record`` line's.
    timestamp: datetime
    # From the record: the compaction turn has no ``turn_context``.
    turn_id: str
    model: str | None
    usage: CodexUsage
    is_compaction: bool = False
    # Turns of the prompts (user messages with a ``UserMessage`` item) in the
    # consumed round; not the injected ``<turn_aborted>`` message.
    consumed_turn_ids: tuple[str, ...] = ()
    # ``item_completed`` items of the tool calls whose outputs are in the consumed round.
    consumed_items: tuple[CodexItem, ...] = ()
    # Turns whose ``task_complete``/``turn_aborted`` was read up to this response.
    finished_turn_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Pending:
    item: ResponsesItem
    # Model-produced since the last ``token_usage_record``.
    output: bool
    turn_id: str | None
    # A user message followed by its ``UserMessage`` item.
    prompt: bool = False


@dataclass(frozen=True)
class _Held:
    """A record with no output items: a compaction if a ``compacted`` names it next."""

    record: TokenUsageRecord
    timestamp: datetime
    index: int
    inherited: bool


def _is_model_output(item: ResponsesItem) -> bool:
    match item:
        case ResponsesMessage():
            return item.role == "assistant"
        case (
            ResponsesReasoning()
            | ResponsesFunctionCall()
            | ResponsesCustomToolCall()
            | ResponsesWebSearchCall()
            | ResponsesCompaction()
        ):
            return True
        case ResponsesUnknownItem():
            return item.type.endswith("_call")
    return False


def _usage(usage: CodexUsage) -> ResponsesUsage:
    return ResponsesUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        input_tokens_details=ResponsesInputTokensDetails(
            cached_tokens=usage.cached_input_tokens,
            cache_write_tokens=usage.cache_write_input_tokens,
        ),
        output_tokens_details=ResponsesOutputTokensDetails(
            reasoning_tokens=usage.reasoning_output_tokens
        ),
    )


class RolloutCursor:
    """Not thread-safe: callers hold the owning ``Session.lock``."""

    def __init__(self, log: SessionLog) -> None:
        self._log = log
        self._pos = 0
        self.base_instructions: str | None = None
        self.originator: str | None = None
        self.cli_version: str | None = None
        self.model: str | None = None
        self.turn_id: str | None = None
        self.cwd: str | None = None
        self.committed: list[ResponsesItem] = []
        self.pending: list[_Pending] = []
        self._held: _Held | None = None
        self._ready: deque[RolloutInvocation] = deque()
        # Responses closing at or before this line index are not returned.
        self._skip_through = -1
        self._finished: list[str] = []
        # Item join: function mode by call id, script mode by the ``exec`` call
        # whose window the item was read in.
        self._function_calls: set[str] = set()
        self._custom_calls: set[str] = set()
        self._items: dict[str, CodexItem] = {}
        self._script_items: dict[str, list[CodexItem]] = {}
        self._script_call: str | None = None
        self._renamed: set[str] = set()

    @property
    def history_truncated(self) -> bool:
        return self._log.history_truncated

    @property
    def at_end(self) -> bool:
        return self._pos >= len(self._log.lines) and not self._ready

    def next_closed(self) -> RolloutInvocation | None:
        """The next closed response, or ``None`` when the log has nothing more."""
        while not self._ready and self._pos < len(self._log.lines):
            self._step()
        return self._ready.popleft() if self._ready else None

    def skip_to(self, watermark: Checkpoint) -> None:
        """Fold past the watermark's record (by id, else the last record at or
        before its timestamp) without returning it or anything before it.
        Responses inherited from a fork's parent are never returned."""
        own = [
            (index, line.line)
            for index, line in enumerate(self._log.lines)
            if not line.inherited and isinstance(line.line.payload, TokenUsageRecord)
        ]
        by_id = [
            index
            for index, line in own
            if isinstance(line.payload, TokenUsageRecord)
            and line.payload.response_id == watermark.id
        ]
        if watermark.id is not None and by_id:
            self._skip_through = by_id[-1]
        elif watermark.timestamp is not None:
            stamp = watermark.timestamp
            self._skip_through = max(
                (index for index, line in own if line.timestamp <= stamp), default=-1
            )
        while self._pos <= self._skip_through:
            self._step()
        self._ready.clear()

    def advance_to_end(self) -> None:
        while self._pos < len(self._log.lines):
            self._step()
        self._ready.clear()

    def view(self) -> tuple[NormalizedMessage, ...]:
        """The history so far, in-flight items included, as normalized messages."""
        request = ResponsesRequest.model_construct(
            instructions=self.base_instructions,
            input=[*self.committed, *(p.item for p in self.pending)],
            tools=[],
        )
        empty = Response.model_construct(object="response", id="", output=[])
        return tuple(to_normalized(request, empty).input.messages)

    def pending_call(self, tool_use_id: str) -> ResponsesFunctionCall | None:
        """The call with ``tool_use_id``, in flight or already closed."""
        for item in (*reversed([p.item for p in self.pending]), *reversed(self.committed)):
            if isinstance(item, ResponsesFunctionCall) and item.call_id == tool_use_id:
                return item
        return None

    def items_for(self, call_id: str) -> tuple[CodexItem, ...]:
        """``item_completed`` items joined to ``call_id`` that no closed
        response has consumed yet."""
        own = self._items.get(call_id)
        return (*(() if own is None else (own,)), *self._script_items.get(call_id, ()))

    # ----------------------------------------------------------------------
    # Folding
    # ----------------------------------------------------------------------

    def _step(self) -> None:
        index = self._pos
        log_line = self._log.lines[index]
        self._pos += 1
        payload = log_line.line.payload
        if self._held is not None:
            held, self._held = self._held, None
            if (
                isinstance(payload, Compacted)
                and payload.compaction_response_id == held.record.response_id
            ):
                compaction = next(
                    (
                        i
                        for i in reversed(payload.replacement_history)
                        if isinstance(i, ResponsesCompaction)
                    ),
                    None,
                )
                output = ResponsesCompaction(
                    type="compaction",
                    encrypted_content=compaction.encrypted_content if compaction else None,
                )
                self._close(held, [output], is_compaction=True)
                return
            self._close(held, [], is_compaction=False)
        match payload:
            case SessionMeta():
                if payload.base_instructions is not None:
                    self.base_instructions = payload.base_instructions.text
                self.originator = payload.originator or self.originator
                self.cli_version = payload.cli_version or self.cli_version
            case TurnContext():
                self.turn_id = payload.turn_id
                self.model = payload.model or self.model
                self.cwd = payload.cwd
            case TaskStarted():
                self.turn_id = payload.turn_id or self.turn_id
            case TokenUsageRecord():
                held = _Held(payload, log_line.line.timestamp, index, log_line.inherited)
                if any(p.output for p in self.pending):
                    self._close(held, None, is_compaction=False)
                else:
                    self._held = held
            case Compacted():
                pass
            case ItemCompleted():
                self._on_item(payload.item)
            case TurnAborted():
                self._abort(payload.turn_id)
            case TaskComplete():
                if payload.turn_id is not None:
                    self._finished.append(payload.turn_id)
            case _:
                self._on_response_item(payload)

    def _on_response_item(self, item: ResponsesItem) -> None:
        match item:
            case ResponsesFunctionCall():
                self._function_calls.add(item.call_id)
                item = map_function_call(item)
            case ResponsesCustomToolCall():
                self._custom_calls.add(item.call_id)
                if item.name == SCRIPT_TOOL:
                    # Parallel ``exec`` calls: their items all join the last one.
                    self._script_call = item.call_id
                if (mapped := map_custom_call(item, self.cwd)) is not None:
                    self._renamed.add(item.call_id)
                    item = mapped
            case ResponsesCustomToolCallOutput() if item.call_id in self._renamed:
                item = ResponsesFunctionCallOutput(
                    type="function_call_output", call_id=item.call_id, output=item.output
                )
        self.pending.append(_Pending(item, _is_model_output(item), self.turn_id))

    def _on_item(self, item: CodexItem) -> None:
        if isinstance(item, UserMessageItem):
            last = self.pending[-1] if self.pending else None
            if (
                last is not None
                and isinstance(last.item, ResponsesMessage)
                and last.item.role == "user"
            ):
                self.pending[-1] = replace(last, prompt=True)
            return
        if item.id is None:
            return
        if isinstance(item, OtherItem) and item.type in _NOT_TOOL_ITEMS:
            return
        if item.id in self._function_calls or self._script_call is None:
            self._items[item.id] = item
        else:
            self._script_items.setdefault(self._script_call, []).append(item)

    def _abort(self, turn_id: str | None) -> None:
        """The unclosed response is never emitted: its reasoning and text go,
        its calls and their results stay for the next response."""
        self.pending = [
            replace(p, output=False)
            for p in self.pending
            if not (p.output and isinstance(p.item, ResponsesMessage | ResponsesReasoning))
        ]
        if turn_id is not None:
            self._finished.append(turn_id)

    def _close(
        self, held: _Held, output: list[ResponsesItem] | None, *, is_compaction: bool
    ) -> None:
        """``output=None``: the model-produced pending items. What precedes
        them is the consumed round; tool results written after them carry over."""
        if output is None:
            first = next(i for i, p in enumerate(self.pending) if p.output)
            consumed = [p for p in self.pending[:first] if not p.output]
            output = [p.item for p in self.pending if p.output]
            carried = [p for p in self.pending[first:] if not p.output]
        else:
            consumed, carried = self.pending, []
        round_items = [p.item for p in consumed]
        request = ResponsesRequest.model_construct(
            instructions=self.base_instructions, input=[*self.committed, *round_items], tools=[]
        )
        response = Response.model_construct(
            object="response",
            id=held.record.response_id,
            status="completed",
            output=output,
            usage=_usage(held.record.usage),
            incomplete_details=None,
            model=None,
        )
        invocation = RolloutInvocation(
            request=request,
            response=response,
            response_id=held.record.response_id,
            timestamp=held.timestamp,
            turn_id=held.record.turn_id,
            model=self.model,
            usage=held.record.usage,
            is_compaction=is_compaction,
            consumed_turn_ids=tuple(
                dict.fromkeys(p.turn_id for p in consumed if p.prompt and p.turn_id is not None)
            ),
            consumed_items=self._join(round_items),
            finished_turn_ids=tuple(self._finished),
        )
        self._finished = []
        self.committed.extend(round_items)
        self.committed.extend(output)
        self.pending = carried
        if not held.inherited and held.index > self._skip_through:
            self._ready.append(invocation)

    def _join(self, round_items: list[ResponsesItem]) -> tuple[CodexItem, ...]:
        joined: list[CodexItem] = []
        call_ids = dict.fromkeys(
            i.call_id
            for i in round_items
            if isinstance(i, ResponsesFunctionCallOutput | ResponsesCustomToolCallOutput)
        )
        for call_id in call_ids:
            if (item := self._items.pop(call_id, None)) is not None:
                joined.append(item)
            joined.extend(self._script_items.pop(call_id, []))
            self._function_calls.discard(call_id)
            self._custom_calls.discard(call_id)
            if self._script_call == call_id:
                self._script_call = None
        # Keep only what calls still awaiting consumption can join.
        open_calls = self._function_calls | self._custom_calls
        self._items = {k: v for k, v in self._items.items() if k in open_calls}
        self._script_items = {k: v for k, v in self._script_items.items() if k in open_calls}
        self._renamed &= open_calls
        return tuple(joined)
