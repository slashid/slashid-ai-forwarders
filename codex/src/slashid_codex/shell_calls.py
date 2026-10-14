"""Codex's function-mode shell calls as the hooks name them: ``Bash`` with
``{command, workdir}``, and the other tools a script calls once.

Script mode calls ``exec`` with JavaScript whose ``tools.NAME(ARG)`` is function
mode's ``NAME``. Shell commands, image views and MCP calls of a script come
from their ``item_completed`` items instead (``item_calls.py``).
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, JsonValue, ValidationError
from slashid_ai_forwarder_core.normalize._base import _LenientModel
from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    ResponsesCustomToolCall,
    ResponsesFunctionCall,
)

SHELL_TOOL = "exec_command"
SCRIPT_TOOL = "exec"
_FROM_ITEMS = frozenset({SHELL_TOOL, "view_image"})
BASH = "Bash"

_PRAGMA = re.compile(r"\A\s*//\s*@exec:[^\n]*\n")
_ID = r"[A-Za-z_$][\w$]*"
_NAME = rf"({_ID})"
_WRAPPED = re.compile(rf"\Atext\(\s*await\s+tools\.{_NAME}\((.*)\)\s*\)\s*;?\s*\Z", re.S)
_BARE = re.compile(rf"\A(?:await\s+)?tools\.{_NAME}\((.*)\)\s*;?\s*\Z", re.S)
_IDENTIFIER = re.compile(_ID)


class _ExecCommandArgs(_LenientModel):
    cmd: str
    workdir: str | None = None


class _BashInput(BaseModel):
    command: str
    workdir: str | None = None


def _bash(call_id: str, cmd: str, workdir: str | None) -> ResponsesFunctionCall:
    arguments = _BashInput(command=cmd, workdir=workdir).model_dump_json(exclude_none=True)
    return ResponsesFunctionCall(
        type="function_call", call_id=call_id, name=BASH, arguments=arguments
    )


def map_function_call(call: ResponsesFunctionCall) -> ResponsesFunctionCall:
    """``exec_command`` → ``Bash``; anything else unchanged."""
    if call.name != SHELL_TOOL:
        return call
    try:
        args = _ExecCommandArgs.model_validate_json(call.arguments)
    except ValidationError:
        return call
    return _bash(call.call_id, args.cmd, args.workdir)


def map_custom_call(call: ResponsesCustomToolCall) -> ResponsesFunctionCall | None:
    """An ``exec`` script that is a single ``tools.NAME(ARG)`` call (see
    ``parse_script``) → that call; ``None`` for any other script, and for the
    tools whose ``item_completed`` items give the call: shell commands, image
    views and MCP tools (``item_calls.py``)."""
    if call.name != SCRIPT_TOOL or (parsed := parse_script(call.input)) is None:
        return None
    name, arg = parsed
    if name in _FROM_ITEMS or name.startswith("mcp__"):
        return None
    arguments = arg if isinstance(arg, str) else json.dumps(arg)
    return ResponsesFunctionCall(
        type="function_call", call_id=call.call_id, name=name, arguments=arguments
    )


def parse_script(script: str) -> tuple[str, JsonValue] | None:
    """``(NAME, ARG)`` of a script that is one ``tools.NAME(ARG)`` call, bare or
    in ``text(await …)``, after an optional ``// @exec:`` line. ``ARG`` is a
    string or an object literal."""
    body = _PRAGMA.sub("", script, count=1).strip()
    if match := _WRAPPED.match(body) or _BARE.match(body):
        name, arg = match.group(1), match.group(2)
    else:
        return None
    value = _argument(arg.strip())
    return None if value is None else (name, value)


def _argument(text: str) -> JsonValue:
    for candidate in (text, _quote_keys(text)):
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str | dict):
            return value
    return None


def _quote_keys(text: str) -> str:
    """Quote an object literal's bare keys, leaving string contents alone."""
    out: list[str] = []
    i = 0
    expect_key = False
    while i < len(text):
        char = text[i]
        if char == '"':
            end = i + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[i : end + 1])
            i = end + 1
            expect_key = False
            continue
        if expect_key and (key := _IDENTIFIER.match(text, i)):
            rest = text[key.end() :].lstrip()
            if rest.startswith(":"):
                out.append(f'"{key.group(0)}"')
                i = key.end()
                expect_key = False
                continue
        if char in "{,":
            expect_key = True
        elif not char.isspace():
            expect_key = False
        out.append(char)
        i += 1
    return "".join(out)
