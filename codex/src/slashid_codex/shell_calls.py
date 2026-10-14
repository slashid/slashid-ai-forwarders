"""Codex's function-mode shell calls as the hooks name them: ``Bash`` with
``{command, workdir}``.

Script mode calls ``exec`` with JavaScript whose ``tools.NAME(ARG)`` is function
mode's ``NAME``; the tools a script calls come from their ``item_completed``
items instead (``item_calls.py``).
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError
from slashid_ai_forwarder_core.normalize._base import _LenientModel
from slashid_ai_forwarder_core.normalize.openai.responses.schema import ResponsesFunctionCall

SHELL_TOOL = "exec_command"
SCRIPT_TOOL = "exec"
BASH = "Bash"


class _ExecCommandArgs(_LenientModel):
    cmd: str
    workdir: str | None = None


class _BashInput(BaseModel):
    command: str
    workdir: str | None = None


def map_function_call(call: ResponsesFunctionCall) -> ResponsesFunctionCall:
    """``exec_command`` → ``Bash``; anything else unchanged."""
    if call.name != SHELL_TOOL:
        return call
    try:
        args = _ExecCommandArgs.model_validate_json(call.arguments)
    except ValidationError:
        return call
    arguments = _BashInput(command=args.cmd, workdir=args.workdir).model_dump_json(
        exclude_none=True
    )
    return ResponsesFunctionCall(
        type="function_call", call_id=call.call_id, name=BASH, arguments=arguments
    )
