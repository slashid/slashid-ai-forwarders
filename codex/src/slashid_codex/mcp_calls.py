"""``codex_apps`` MCP calls a script made, as the ``function_call`` pairs the
shared normalizers read. The name is the one the ``PreToolUse`` hook reports
(``mcp__codex_apps__google_drive__fetch``), not the script's, so the
preflight's ``requested_tool_uses`` and the event's ``used_tools`` share ids."""

from __future__ import annotations

import json
from collections.abc import Iterable

from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    ResponsesFunctionCall,
    ResponsesFunctionCallOutput,
    ResponsesInputText,
)

from .rollout import McpToolCall

CODEX_APPS = "codex_apps"
APPS_PREFIX = f"mcp__{CODEX_APPS}__"


def tool_name(item: McpToolCall) -> str:
    return f"mcp__{item.server}__{item.tool.replace('.', '__')}"


def failed(item: McpToolCall) -> bool:
    """Any status but ``completed`` (the only one captured), or ``isError``."""
    if item.status not in (None, "completed"):
        return True
    return item.result is not None and item.result.is_error


def _result_json(item: McpToolCall) -> str:
    if item.result is None:
        return json.dumps({"isError": failed(item)})
    return item.result.model_dump_json(by_alias=True)


def pairs(
    items: Iterable[McpToolCall],
) -> tuple[list[ResponsesFunctionCall], list[ResponsesFunctionCallOutput]]:
    """The calls and the outputs, in ``items`` order, for ``codex_apps`` items."""
    calls: list[ResponsesFunctionCall] = []
    outputs: list[ResponsesFunctionCallOutput] = []
    for item in items:
        if item.server != CODEX_APPS:
            continue
        arguments = json.dumps({} if item.arguments is None else item.arguments)
        calls.append(
            ResponsesFunctionCall(
                type="function_call",
                call_id=item.id,
                name=tool_name(item),
                arguments=arguments,
                synthetic=True,
            )
        )
        outputs.append(
            ResponsesFunctionCallOutput(
                type="function_call_output",
                call_id=item.id,
                output=[ResponsesInputText(type="input_text", text=_result_json(item))],
                synthetic=True,
            )
        )
    return calls, outputs
