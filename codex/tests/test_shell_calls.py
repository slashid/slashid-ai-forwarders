from __future__ import annotations

import json

import pytest
from slashid_ai_forwarder_core.normalize.openai.responses.schema import ResponsesFunctionCall

from slashid_codex.shell_calls import map_function_call


def _function(name: str, arguments: str) -> ResponsesFunctionCall:
    return ResponsesFunctionCall(
        type="function_call", call_id="call_1", name=name, arguments=arguments
    )


def test_exec_command_becomes_bash_with_command_and_workdir() -> None:
    call = map_function_call(
        _function(
            "exec_command",
            '{"cmd": "sed -n \'1,240p\' notes.md", "workdir": "/home/user/project", '
            '"yield_time_ms": 10000, "max_output_tokens": 12000}',
        )
    )
    assert call.name == "Bash"
    assert call.call_id == "call_1"
    assert json.loads(call.arguments) == {
        "command": "sed -n '1,240p' notes.md",
        "workdir": "/home/user/project",
    }


def test_exec_command_without_workdir_omits_it() -> None:
    call = map_function_call(_function("exec_command", '{"cmd": "ls"}'))
    assert json.loads(call.arguments) == {"command": "ls"}


@pytest.mark.parametrize(
    ("name", "arguments"),
    [("view_image", '{"path": "/x.png"}'), ("exec_command", "not json"), ("exec_command", "{}")],
)
def test_other_function_calls_unchanged(name: str, arguments: str) -> None:
    call = _function(name, arguments)
    assert map_function_call(call) is call
