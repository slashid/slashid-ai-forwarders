"""One tool call as either surface saw it, its files and the event of a check."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue
from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AIToolUse,
    AWSIdentityDetails,
)
from slashid_ai_forwarder_core.normalize.finalize import merge_by_refs
from slashid_ai_forwarder_core.normalize.normalized.connector_files import (
    connector_files,
    connector_refs,
)
from slashid_ai_forwarder_core.normalize.normalized.tool_results import (
    connector_accessed_file,
    connector_stub,
)
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool


class ToolCall(BaseModel):
    """One call as either surface saw it."""

    model_config = ConfigDict(frozen=True)

    surface: Literal["harness", "gateway"]
    raw_name: str  # as the platform gave it; connector recognition uses this
    resolved_name: str  # names.resolve_name(...); tool/server identity only
    call_id: str
    tool_input: JsonValue
    request_id: str
    conversation_id: str | None
    user_agent: str | None
    identity: AWSIdentityDetails

    @property
    def parsed_as(self) -> str:
        return f"agentcore-{self.surface}-hook"


class CachedInput(BaseModel):
    """What an allowed ``before_tool_call`` leaves for its ``after_tool_call``."""

    model_config = ConfigDict(frozen=True)

    tool_name: str
    tool_input: JsonValue

    def matches(self, call: ToolCall) -> bool:
        return self.tool_name == call.raw_name


def call_files(call: ToolCall) -> list[AIAccessedFile]:
    """Reference stubs from the call's arguments."""
    refs = connector_refs(call.raw_name, call.tool_input)
    return merge_by_refs([connector_stub(cf) for cf in refs])


def result_files(
    call: ToolCall, tool_input: JsonValue, output: JsonValue, *, config: BaseConfig
) -> list[AIAccessedFile]:
    """``output`` is the Gateway ``result`` object or the Harness ``content`` list."""
    files = connector_files(call.raw_name, tool_input, output)
    return merge_by_refs([connector_accessed_file(cf, config=config) for cf in files])


def build_event(
    call: ToolCall,
    *,
    result_is_error: bool | None,
    files: list[AIAccessedFile],
    now: datetime | None = None,
) -> AIInvocationObservedV1:
    """``result_is_error`` None builds a call check, a bool a result check."""
    tool, server = resolve_tool(call.resolved_name)
    use = AIToolUse(tool_id=tool.id, tool_use_id=call.call_id, is_error=result_is_error)
    return AIInvocationObservedV1(
        request_id=call.request_id,
        timestamp=(now or datetime.now(UTC)).isoformat(),
        identity_details=call.identity,
        model=AIModel(id="unknown"),
        parsed_as=call.parsed_as,
        user_agent=call.user_agent,
        conversation_id=call.conversation_id,
        requested_tool_uses=[use] if result_is_error is None else None,
        used_tools=[use] if result_is_error is not None else None,
        accessed_files=files or None,
        available_tools=[tool],
        available_tool_servers=[server],
    )
