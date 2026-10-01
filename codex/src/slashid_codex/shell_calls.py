"""Codex's shell calls as the hooks name them: ``Bash`` with ``{command, workdir}``.

Function mode calls ``exec_command``; script mode calls ``exec`` with
JavaScript whose ``tools.NAME(ARG)`` is function mode's ``NAME``.
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
BASH = "Bash"

_PRAGMA = re.compile(r"\A\s*//\s*@exec:[^\n]*\n")
_NAME = r"([A-Za-z_$][\w$]*)"
_WRAPPED = re.compile(rf"\Atext\(\s*await\s+tools\.{_NAME}\((.*)\)\s*\)\s*;?\s*\Z", re.S)
_BARE = re.compile(rf"\A(?:await\s+)?tools\.{_NAME}\((.*)\)\s*;?\s*\Z", re.S)
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*")


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


def map_custom_call(call: ResponsesCustomToolCall, cwd: str | None) -> ResponsesFunctionCall | None:
    """An ``exec`` script that is a single ``tools.NAME(ARG)`` call → that call;
    ``None`` for any other script. ``cwd`` (the turn's) stands in for a
    missing ``workdir``."""
    if call.name != SCRIPT_TOOL or (parsed := parse_script(call.input)) is None:
        return None
    name, arg = parsed
    if name == SHELL_TOOL:
        try:
            args = _ExecCommandArgs.model_validate(arg)
        except ValidationError:
            return None
        return _bash(call.call_id, args.cmd, args.workdir or cwd)
    arguments = arg if isinstance(arg, str) else json.dumps(arg)
    return ResponsesFunctionCall(
        type="function_call", call_id=call.call_id, name=name, arguments=arguments
    )


def parse_script(script: str) -> tuple[str, JsonValue] | None:
    """``(NAME, ARG)`` of a script that is one ``tools.NAME(ARG)`` call, bare
    or in ``text(await …)``, after an optional ``// @exec:`` line. ``ARG`` is
    a string or an object literal."""
    body = _PRAGMA.sub("", script, count=1).strip()
    match = _WRAPPED.match(body) or _BARE.match(body)
    if match is None:
        return None
    arg = _argument(match.group(2).strip())
    return None if arg is None else (match.group(1), arg)


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
