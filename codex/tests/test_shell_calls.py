from __future__ import annotations

import json

import pytest
from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    ResponsesCustomToolCall,
    ResponsesFunctionCall,
)

from slashid_codex.shell_calls import map_custom_call, map_function_call, parse_script


def _function(name: str, arguments: str) -> ResponsesFunctionCall:
    return ResponsesFunctionCall(
        type="function_call", call_id="call_1", name=name, arguments=arguments
    )


def _exec(script: str) -> ResponsesCustomToolCall:
    return ResponsesCustomToolCall(
        type="custom_tool_call", call_id="call_2", name="exec", input=script
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


def test_other_tools_keep_their_arguments() -> None:
    call = map_custom_call(_exec('text(await tools.update_plan({steps: ["a"], "full": true}));'))
    assert call is not None
    assert call.name == "update_plan"
    assert call.call_id == "call_2"
    assert json.loads(call.arguments) == {"steps": ["a"], "full": True}


def test_string_argument() -> None:
    call = map_custom_call(_exec('text(await tools.apply_patch("*** Begin Patch\\n"));'))
    assert call is not None
    assert call.name == "apply_patch"
    assert call.arguments == "*** Begin Patch\n"


@pytest.mark.parametrize(
    "script",
    [
        'text(await tools.exec_command({cmd:"cat note.txt"}));',
        'const r = await tools.view_image({path:"/abs/img.png"}); image(r.image_url);\n',
        'text(await tools.mcp__payroll__read({employee_id: "e-1"}));',
        'text(await tools.mcp__codex_apps__google_drive_fetch({url: "u"}));',
    ],
)
def test_calls_with_items_are_left_to_them(script: str) -> None:
    assert map_custom_call(_exec(script)) is None


@pytest.mark.parametrize(
    "script",
    [
        'text(await tools.update_plan({a:"a"})); text(await tools.update_plan({a:"b"}));',
        'const r = await tools.update_plan({a:"a"}); text(r);',
        "text(await tools.update_plan({a: `a`}));",
        "text(await tools.update_plan());",
        'text("hello");',
    ],
)
def test_other_scripts_do_not_match(script: str) -> None:
    assert map_custom_call(_exec(script)) is None


def test_quoting_ignores_keys_inside_strings() -> None:
    assert parse_script('tools.exec_command({cmd:"echo a,b:c"})') == (
        "exec_command",
        {"cmd": "echo a,b:c"},
    )


def test_non_exec_custom_tool_not_mapped() -> None:
    call = ResponsesCustomToolCall(
        type="custom_tool_call", call_id="c", name="apply_patch", input="tools.x({})"
    )
    assert map_custom_call(call) is None
