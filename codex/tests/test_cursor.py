from __future__ import annotations

import json
import shutil
from datetime import timedelta
from pathlib import Path

from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    ResponsesCompaction,
    ResponsesFunctionCall,
    ResponsesFunctionCallOutput,
    ResponsesInputText,
    ResponsesItem,
    ResponsesMessage,
    ResponsesReasoning,
)
from slashid_ai_forwarder_core.platform.checkpoint import Checkpoint

from slashid_codex.cursor import RolloutCursor, RolloutInvocation
from slashid_codex.log import SessionLog
from slashid_codex.rollout import CommandExecution, ImageView, TokenUsageRecord, parse_line

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
PARENT_ID = "01a0f553-7026-70e1-ae0c-d833daddaa9e"
EMPTY = Checkpoint(timestamp=None, id=None)


def _raw(name: str) -> list[bytes]:
    return (ROLLOUTS / f"{name}.jsonl").read_bytes().splitlines(keepends=True)


def _records(name: str) -> list[tuple[int, TokenUsageRecord]]:
    out = []
    for index, raw in enumerate(_raw(name)):
        line = parse_line(raw)
        if line is not None and isinstance(line.payload, TokenUsageRecord):
            out.append((index, line.payload))
    return out


def _log(tmp_path: Path, name: str, *, upto: int | None = None, parent: bool = True) -> SessionLog:
    path = tmp_path / f"{name}.jsonl"
    path.write_bytes(b"".join(_raw(name)[:upto]))
    parent_path = tmp_path / "parent.jsonl"
    shutil.copy(ROLLOUTS / "compaction.jsonl", parent_path)

    def locate(thread_id: str) -> Path | None:
        return parent_path if parent and thread_id == PARENT_ID else None

    log = SessionLog.open(path, locate)
    log.refresh()
    return log


def _drain(cursor: RolloutCursor) -> list[RolloutInvocation]:
    out = []
    while (invocation := cursor.next_closed()) is not None:
        out.append(invocation)
    return out


def _calls(items: list[ResponsesItem]) -> list[ResponsesFunctionCall]:
    return [i for i in items if isinstance(i, ResponsesFunctionCall)]


def _inputs(invocation: RolloutInvocation) -> list[ResponsesItem]:
    assert isinstance(invocation.request.input, list)
    return invocation.request.input


def test_script_mode(tmp_path: Path) -> None:
    responses = _drain(RolloutCursor(_log(tmp_path, "script")))
    assert [r.response_id for r in responses] == [r.response_id for _, r in _records("script")]
    first, second = responses
    assert first.request.instructions == "<instructions>"
    assert first.model == "gpt-6-astra"
    assert first.turn_id == "01a0f397-f1ce-7640-a04b-370d7af13c6f"
    assert first.consumed_turn_ids == ("01a0f397-f1ce-7640-a04b-370d7af13c6f",)
    assert first.timestamp.isoformat() == "2026-09-30T18:33:45.307000+00:00"
    assert first.usage.cache_write_input_tokens == 15186
    assert first.response.usage is not None
    assert first.response.usage.input_tokens == 15189
    [call] = _calls(first.response.output)
    assert call.name == "Bash"
    assert json.loads(call.arguments) == {"command": "cat note.txt", "workdir": "/home/user/work"}
    # The exec item precedes the output; it joins the round that consumes the output.
    assert first.consumed_items == ()
    [item] = second.consumed_items
    assert isinstance(item, CommandExecution)
    assert item.id.startswith("exec-")
    outputs = [i for i in _inputs(second) if isinstance(i, ResponsesFunctionCallOutput)]
    assert [o.call_id for o in outputs] == [call.call_id]
    assert second.finished_turn_ids == ()


def test_function_mode(tmp_path: Path) -> None:
    responses = _drain(RolloutCursor(_log(tmp_path, "function")))
    assert len(responses) == 5
    [sed] = _calls(responses[1].response.output)
    assert sed.name == "Bash"
    assert json.loads(sed.arguments) == {
        "command": "sed -n '1,240p' /home/user/Recipes/notes.md",
        "workdir": "/home/user/project",
    }
    [view] = _calls(responses[3].response.output)
    assert view.name == "view_image"
    assert json.loads(view.arguments)["path"] == "/home/user/Documentos/image 1.png"

    [command] = responses[2].consumed_items
    assert isinstance(command, CommandExecution)
    assert command.id == sed.call_id
    assert command.exit_code == 0
    # ``ImageView`` is written before the calling response's record.
    assert responses[3].consumed_items == ()
    [image] = responses[4].consumed_items
    assert isinstance(image, ImageView)
    assert image.id == view.call_id
    # Attachments turn, read turn, image turn.
    assert responses[0].consumed_turn_ids == ("01a0f44f-553f-7200-891b-509968373955",)
    assert responses[1].consumed_turn_ids == ("01a0f44f-aa7b-7d11-8e31-d803f34b4c61",)
    assert responses[1].finished_turn_ids == ("01a0f44f-553f-7200-891b-509968373955",)
    assert responses[2].consumed_turn_ids == ()


def test_interrupted_response_is_dropped_but_its_tool_results_stay(tmp_path: Path) -> None:
    responses = _drain(RolloutCursor(_log(tmp_path, "interrupt")))
    assert [r.response_id for r in responses] == [r.response_id for _, r in _records("interrupt")]
    parallel = responses[2]
    calls = _calls(parallel.response.output)
    assert len(calls) == 4
    assert {c.name for c in calls} == {"Bash"}

    after = responses[3]
    inputs = _inputs(after)
    outputs = [i.call_id for i in inputs if isinstance(i, ResponsesFunctionCallOutput)]
    assert {c.call_id for c in calls} <= set(outputs)
    assert [i.id for i in after.consumed_items if isinstance(i, CommandExecution)] == [
        c.call_id for c in calls
    ]
    texts = [
        p.text
        for i in inputs
        if isinstance(i, ResponsesMessage) and isinstance(i.content, list)
        for p in i.content
        if isinstance(p, ResponsesInputText)
    ]
    assert any(t.startswith("<turn_aborted>") for t in texts)
    # The aborted response's reasoning is not in the history.
    reasoning = [i for i in inputs if isinstance(i, ResponsesReasoning)]
    assert len(reasoning) == len(
        [i for r in responses[:3] for i in r.response.output if isinstance(i, ResponsesReasoning)]
    )
    aborted = "01a0f392-6406-7d82-8234-af07c8203a7c"
    assert after.finished_turn_ids == (aborted,)
    assert after.consumed_turn_ids == (aborted, "01a0f43a-57b1-79b3-955a-5a33f4db0efc")
    # Rounds already returned are not changed by the abort.
    assert len(_calls(parallel.response.output)) == 4


def test_compaction(tmp_path: Path) -> None:
    responses = _drain(RolloutCursor(_log(tmp_path, "compaction")))
    assert [r.response_id for r in responses] == [r.response_id for _, r in _records("compaction")]
    compaction = responses[2]
    assert [r.is_compaction for r in responses] == [False, False, True, False, False]
    assert compaction.response.output == [
        ResponsesCompaction(type="compaction", encrypted_content="COMPACTED-1")
    ]
    assert compaction.turn_id == "01a0f553-ee0f-71c1-b101-07b609a4a08e"
    assert compaction.model == "gpt-5.5"
    # The history stays the logical one: answers from before the compaction remain.
    later = _inputs(responses[3])
    assert responses[0].response.output[0] in later
    assert compaction.response.output[0] in later


def test_log_ending_on_a_compaction_record_holds_it(tmp_path: Path) -> None:
    index, record = _records("compaction")[2]
    log = _log(tmp_path, "compaction", upto=index + 1)
    cursor = RolloutCursor(log)
    assert len(_drain(cursor)) == 2
    assert cursor.next_closed() is None
    with log.path.open("ab") as f:
        f.write(b"".join(_raw("compaction")[index + 1 :]))
    log.refresh()
    held = cursor.next_closed()
    assert held is not None
    assert held.response_id == record.response_id
    assert held.is_compaction


def test_fork_sends_only_its_own_responses(tmp_path: Path) -> None:
    cursor = RolloutCursor(_log(tmp_path, "fork"))
    cursor.skip_to(EMPTY)
    responses = _drain(cursor)
    assert [r.response_id for r in responses] == [r.response_id for _, r in _records("fork")]
    assert not cursor.history_truncated
    # The parent's history is context.
    assert len(_inputs(responses[0])) > len(_inputs(_drain(_cursor_at_start(tmp_path))[0]))


def _cursor_at_start(tmp_path: Path) -> RolloutCursor:
    return RolloutCursor(_log(tmp_path, "fork", parent=False))


def test_fork_with_missing_parent(tmp_path: Path) -> None:
    cursor = _cursor_at_start(tmp_path)
    cursor.skip_to(EMPTY)
    assert cursor.history_truncated
    assert [r.response_id for r in _drain(cursor)] == [r.response_id for _, r in _records("fork")]


def _timestamps(name: str) -> list:
    out = []
    for index, _ in _records(name):
        line = parse_line(_raw(name)[index])
        assert line is not None
        out.append(line.timestamp)
    return out


def test_skip_to_by_id(tmp_path: Path) -> None:
    ids = [r.response_id for _, r in _records("function")]
    cursor = RolloutCursor(_log(tmp_path, "function"))
    cursor.skip_to(Checkpoint(timestamp=_timestamps("function")[1], id=ids[1]))
    assert [r.response_id for r in _drain(cursor)] == ids[2:]


def test_skip_to_by_timestamp_when_the_id_is_gone(tmp_path: Path) -> None:
    ids = [r.response_id for _, r in _records("function")]
    stamps = _timestamps("function")
    cursor = RolloutCursor(_log(tmp_path, "function"))
    cursor.skip_to(Checkpoint(timestamp=stamps[1], id="resp_gone"))
    assert [r.response_id for r in _drain(cursor)] == ids[2:]

    cursor = RolloutCursor(_log(tmp_path, "function"))
    cursor.skip_to(Checkpoint(timestamp=stamps[0] - timedelta(seconds=1), id="resp_gone"))
    assert [r.response_id for r in _drain(cursor)] == ids


def test_skip_to_empty_and_past_a_held_compaction(tmp_path: Path) -> None:
    ids = [r.response_id for _, r in _records("compaction")]
    cursor = RolloutCursor(_log(tmp_path, "compaction"))
    cursor.skip_to(EMPTY)
    assert [r.response_id for r in _drain(cursor)] == ids

    cursor = RolloutCursor(_log(tmp_path, "compaction"))
    cursor.skip_to(Checkpoint(timestamp=_timestamps("compaction")[2], id=ids[2]))
    assert [r.response_id for r in _drain(cursor)] == ids[3:]


def test_view_and_pending_call_see_the_in_flight_call(tmp_path: Path) -> None:
    raw = _raw("function")
    index = next(i for i, line in enumerate(raw) if b'"name":"exec_command"' in line)
    cursor = RolloutCursor(_log(tmp_path, "function", upto=index + 1))
    cursor.advance_to_end()
    assert cursor.at_end
    view = cursor.view()
    assert view[0].role == "system"
    last = view[-1]
    assert last.role == "assistant"
    [tool_use] = [b for b in last.content if b.kind == "tool_use"]
    assert tool_use.tool_name == "Bash"
    assert tool_use.tool_use_id == "call_PaTVhmRLsPDp4UUH9JaOFRUl"

    call = cursor.pending_call("call_PaTVhmRLsPDp4UUH9JaOFRUl")
    assert call is not None
    assert json.loads(call.arguments)["workdir"] == "/home/user/project"
    assert cursor.pending_call("call_unknown") is None


def test_view_after_responses_closed(tmp_path: Path) -> None:
    cursor = RolloutCursor(_log(tmp_path, "function"))
    cursor.advance_to_end()
    assert cursor.next_closed() is None
    view = cursor.view()
    assert view[-1].role == "assistant"
    assert cursor.pending_call("call_rf24Kdk5IOcyNLeC1kBcXjhi") is not None
