"""Tool calls a script made, as the ``function_call`` pairs the shared
normalizers read. Codex records one ``item_completed`` per call (an MCP tool,
a shell command, an image view) whatever the script looked like.

Names are the ones the ``PreToolUse`` hook reports (``mcp__codex_apps__google_drive__fetch``,
``Bash``, ``view_image``), so the preflight's ``requested_tool_uses`` and the
event's ``used_tools`` share ids."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable
from urllib.parse import unquote, urlparse

from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    ResponsesFunctionCall,
    ResponsesFunctionCallOutput,
    ResponsesInputText,
    ResponsesPart,
)

from .rollout import CodexItem, CommandExecution, ImageView, McpToolCall

CallItem = McpToolCall | CommandExecution | ImageView
_SHELL = "Bash"
_VIEW_IMAGE = "view_image"
_SHELL_FLAGS = frozenset({"-c", "-lc"})


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


def _file_path(url: str) -> str:
    return unquote(urlparse(url).path)


def _command(argv: list[str]) -> str:
    """The shell command of ``["/bin/bash", "-lc", "cat note.txt"]``, as the hook gives it."""
    if len(argv) == 3 and argv[1] in _SHELL_FLAGS:
        return argv[2]
    return shlex.join(argv)


def _call(item_id: str, name: str, arguments: object) -> ResponsesFunctionCall:
    return ResponsesFunctionCall(
        type="function_call",
        call_id=item_id,
        name=name,
        arguments=json.dumps(arguments),
        synthetic=True,
    )


def _output(item_id: str, text: str | None) -> ResponsesFunctionCallOutput:
    parts: list[ResponsesPart] = (
        [] if text is None else [ResponsesInputText(type="input_text", text=text)]
    )
    return ResponsesFunctionCallOutput(type="function_call_output", call_id=item_id, output=parts)


def _pair(item: CallItem) -> tuple[ResponsesFunctionCall, ResponsesFunctionCallOutput]:
    match item:
        case McpToolCall():
            arguments = {} if item.arguments is None else item.arguments
            return _call(item.id, tool_name(item), arguments), _output(item.id, _result_json(item))
        case CommandExecution():
            arguments = {"command": _command(item.command)}
            if item.cwd:
                arguments["workdir"] = _file_path(item.cwd)
            return _call(item.id, _SHELL, arguments), _output(item.id, item.formatted_output)
        case ImageView():
            return _call(item.id, _VIEW_IMAGE, {"path": _file_path(item.path)}), _output(
                item.id, None
            )


def pairs(
    items: Iterable[CodexItem],
) -> tuple[list[ResponsesFunctionCall], list[ResponsesFunctionCallOutput]]:
    """The calls and the outputs, in ``items`` order, for the items that are calls."""
    calls: list[ResponsesFunctionCall] = []
    outputs: list[ResponsesFunctionCallOutput] = []
    for item in items:
        if isinstance(item, McpToolCall | CommandExecution | ImageView):
            call, output = _pair(item)
            calls.append(call)
            outputs.append(output)
    return calls, outputs
