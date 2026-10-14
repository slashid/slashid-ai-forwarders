"""A closed rollout response → ``AIInvocationObservedV1`` (spec "Event")."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AIToolServer,
    EventEnvelope,
    OpenAIIdentityDetails,
    build_event_from_normalized,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.openai.responses.normalize import to_normalized

from .config import CodexConfig
from .cursor import RolloutInvocation
from .envelope import declared_from_calls, failed_calls, mark_errors, wire_time
from .state import FileRecordStore
from .usage import codex_usage_to_tokens

PARSED_AS = "codex-rollout"
PARSED_AS_COMPACTION = "codex-compaction"


@dataclass(frozen=True)
class SessionContext:
    session_id: str
    originator: str | None = None
    cli_version: str | None = None
    history_truncated: bool = False
    mcp_servers: Sequence[AIToolServer] = ()

    @property
    def user_agent(self) -> str | None:
        if self.originator and self.cli_version:
            return f"{self.originator}/{self.cli_version}"
        return self.originator


def record_keys(invocation: RolloutInvocation) -> tuple[list[str], list[str]]:
    """The file-record keys the response consumed, as preflight keys them:
    its user messages' turns, and its tool results' call ids with their
    ``item_completed`` ids."""
    items = (item.id for item in invocation.consumed_items if item.id is not None)
    return list(invocation.consumed_turn_ids), list(
        dict.fromkeys([*invocation.consumed_call_ids, *items])
    )


async def build_event(
    invocation: RolloutInvocation,
    context: SessionContext,
    *,
    config: CodexConfig,
    records: FileRecordStore,
) -> AIInvocationObservedV1:
    normalized = to_normalized(invocation.request, invocation.response)
    messages = mark_errors(normalized.input.messages, failed_calls(invocation.consumed_items))
    answer = normalized.output.message
    tools, servers = declared_from_calls([*messages, *([answer] if answer else [])])
    known = {s.id for s in servers}
    servers += [s for s in context.mcp_servers if s.id not in known]
    normalized.input = normalized.input.model_copy(
        update={"messages": messages, "tools_declared": tools, "tool_servers": servers}
    )
    turn_ids, tool_ids = record_keys(invocation)
    normalized.accessed_files = records.for_round(context.session_id, turn_ids, tool_ids)
    finalize(normalized, config=config)
    envelope = EventEnvelope(
        request_id=invocation.response_id,
        timestamp=wire_time(invocation.timestamp),
        identity_details=OpenAIIdentityDetails(user_id=config.user_id),
        model=AIModel(id=invocation.model or "unknown", provider="openai"),
        tokens=codex_usage_to_tokens(invocation.usage),
        parsed_as=PARSED_AS_COMPACTION if invocation.is_compaction else PARSED_AS,
        user_agent=context.user_agent,
        conversation_id=context.session_id,
        history_truncated=context.history_truncated,
    )
    return await build_event_from_normalized(normalized, envelope, config=config)
