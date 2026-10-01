from __future__ import annotations

from pathlib import Path

import pytest
from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    ResponsesCompaction,
    ResponsesCustomToolCall,
    ResponsesFunctionCall,
)

from slashid_codex.rollout import (
    CommandExecution,
    Compacted,
    ImageView,
    ItemCompleted,
    OtherItem,
    RolloutLineError,
    SessionMeta,
    TaskComplete,
    TaskStarted,
    TokenUsageRecord,
    TurnAborted,
    TurnContext,
    UserMessageItem,
    parse_line,
)

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
FIXTURES = ("script", "function", "interrupt", "compaction", "fork")


def _lines(name: str) -> list[bytes]:
    return (ROLLOUTS / f"{name}.jsonl").read_bytes().splitlines()


@pytest.mark.parametrize("name", FIXTURES)
def test_every_fixture_line_parses(name: str) -> None:
    for raw in _lines(name):
        parse_line(raw)


def _parsed(name: str):
    return [line for raw in _lines(name) if (line := parse_line(raw)) is not None]


def test_unmodelled_types_are_none() -> None:
    assert (
        parse_line(b'{"timestamp":"2026-09-30T18:33:37.717Z","type":"world_state","payload":{}}')
        is None
    )
    raw = (
        b'{"timestamp":"2026-09-30T18:33:37.717Z","type":"event_msg",'
        b'"payload":{"type":"thread_settings_applied","thread_id":"t"}}'
    )
    assert parse_line(raw) is None
    raw = (
        b'{"timestamp":"2026-09-30T18:33:37.717Z","type":"event_msg",'
        b'"payload":{"type":"token_count"}}'
    )
    assert parse_line(raw) is None


@pytest.mark.parametrize("payload", [b"[]", b'"x"', b"null"])
def test_unmodelled_types_with_any_payload_are_none(payload: bytes) -> None:
    raw = b'{"timestamp":"2026-09-30T18:33:37.717Z","type":"other","payload":' + payload + b"}"
    assert parse_line(raw) is None


def test_event_msg_with_non_object_payload_raises() -> None:
    with pytest.raises(RolloutLineError):
        parse_line(b'{"timestamp":"2026-09-30T18:33:37.717Z","type":"event_msg","payload":[]}')


def test_broken_json_raises() -> None:
    with pytest.raises(RolloutLineError):
        parse_line(b'{"timestamp": "2026-09-30T18:33:37.717Z", "type": ')


def test_modelled_type_failing_validation_raises() -> None:
    with pytest.raises(RolloutLineError):
        parse_line(
            b'{"timestamp":"2026-09-30T18:33:37.717Z","type":"token_usage_record","payload":{}}'
        )


def test_script_mode_shapes() -> None:
    lines = _parsed("script")
    meta = lines[0].payload
    assert isinstance(meta, SessionMeta)
    assert meta.id == "01a0f397-f16e-7d83-87e7-6701f1b384c7"
    assert meta.originator == "codex_exec"
    assert meta.cli_version == "0.158.0-alpha.2.1"
    assert meta.base_instructions is not None
    assert meta.base_instructions.text == "<instructions>"
    assert meta.history_base is None

    contexts = [line.payload for line in lines if isinstance(line.payload, TurnContext)]
    assert contexts[0].model == "gpt-6-astra"
    assert contexts[0].cwd == "/home/user/work"

    calls = [line.payload for line in lines if isinstance(line.payload, ResponsesCustomToolCall)]
    assert [c.name for c in calls] == ["exec"]

    records = [line.payload for line in lines if isinstance(line.payload, TokenUsageRecord)]
    assert len(records) == 2
    assert records[0].usage.cache_write_input_tokens == 15186

    items = [line.payload.item for line in lines if isinstance(line.payload, ItemCompleted)]
    commands = [i for i in items if isinstance(i, CommandExecution)]
    assert commands[0].id.startswith("exec-")
    assert commands[0].command == ["/bin/bash", "-lc", "cat note.txt"]
    assert commands[0].parsed_cmd[0].type == "read"
    assert commands[0].parsed_cmd[0].path == "note.txt"
    assert commands[0].exit_code == 0
    assert any(isinstance(i, UserMessageItem) for i in items)
    assert any(isinstance(i, OtherItem) and i.type == "AgentMessage" for i in items)

    assert any(isinstance(line.payload, TaskStarted) for line in lines)
    assert any(isinstance(line.payload, TaskComplete) for line in lines)


def test_function_mode_items() -> None:
    lines = _parsed("function")
    items = [line.payload.item for line in lines if isinstance(line.payload, ItemCompleted)]
    views = [i for i in items if isinstance(i, ImageView)]
    assert views[0].id == "call_rf24Kdk5IOcyNLeC1kBcXjhi"
    assert views[0].path == "file:///home/user/Documentos/image%201.png"
    users = [i for i in items if isinstance(i, UserMessageItem)]
    assert any(p.type == "local_image" for p in users[0].content)
    calls = [line.payload for line in lines if isinstance(line.payload, ResponsesFunctionCall)]
    assert [c.name for c in calls] == ["exec_command", "view_image"]


def test_turn_aborted() -> None:
    aborted = [
        line.payload for line in _parsed("interrupt") if isinstance(line.payload, TurnAborted)
    ]
    assert aborted == [
        TurnAborted(
            type="turn_aborted",
            turn_id="01a0f392-6406-7d82-8234-af07c8203a7c",
            reason="interrupted",
        )
    ]


def test_compacted() -> None:
    compacted = [
        line.payload for line in _parsed("compaction") if isinstance(line.payload, Compacted)
    ]
    assert len(compacted) == 1
    assert (
        compacted[0].compaction_response_id
        == "resp_0d2bb4de4651d3da016abdc7a99c7487d28acad097cd8ca81d"
    )
    assert compacted[0].window_number == 1
    last = compacted[0].replacement_history[-1]
    assert isinstance(last, ResponsesCompaction)
    assert last.encrypted_content == "COMPACTED-1"


def test_fork_history_base() -> None:
    meta = _parsed("fork")[0].payload
    assert isinstance(meta, SessionMeta)
    assert meta.forked_from_id == "01a0f553-7026-70e1-ae0c-d833daddaa9e"
    assert meta.history_base is not None
    assert meta.history_base.thread_id == "01a0f553-7026-70e1-ae0c-d833daddaa9e"
    parent = (ROLLOUTS / "compaction.jsonl").read_bytes().splitlines(keepends=True)
    assert meta.history_base.end_byte_offset == sum(len(line) for line in parent[:45])


def test_base_instructions_as_plain_string() -> None:
    raw = (
        b'{"timestamp":"2026-09-30T18:33:37.717Z","type":"session_meta",'
        b'"payload":{"id":"s","base_instructions":"be nice"}}'
    )
    line = parse_line(raw)
    assert line is not None
    assert isinstance(line.payload, SessionMeta)
    assert line.payload.base_instructions is not None
    assert line.payload.base_instructions.text == "be nice"
