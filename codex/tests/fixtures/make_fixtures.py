"""Sanitise captured Codex rollouts into ``rollouts/*.jsonl``.

Run from ``codex/`` on the machine that captured them::

    uv run python tests/fixtures/make_fixtures.py \\
        --script ~/.codex/sessions/…/rollout-…-<script mode>.jsonl \\
        --function ~/.codex/sessions/…/rollout-…-<function mode>.jsonl \\
        --interrupt ~/.codex/archived_sessions/rollout-…-<interrupted>.jsonl \\
        --compaction ~/.codex/sessions/…/rollout-…-<compacted>.jsonl \\
        --fork ~/.codex/sessions/…/rollout-…-<fork of the compacted one>.jsonl \\
        --rename '/home/me/src/repo=/home/user/project' \\
        --rename 'Private Letter.pdf=report.pdf'

Every line keeps its type, ids, timestamps, order and usage. Instructions,
developer and user text, assistant text, tool output, images and encrypted
content become fixed placeholders; tool reads become ``READ_CONTENT``.
``session_meta`` and ``turn_context`` keep only allow-listed keys. Command
text is kept verbatim: captures must not hold sensitive command arguments. The
home directory becomes ``/home/user``, then each ``--rename OLD=NEW`` applies
to every string (and its percent-encoded form), longest first. The fork's
``history_base.end_byte_offset`` is recomputed against the sanitised parent.
The run fails if the home directory's name, an ``OLD`` or an encrypted blob
survives.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from pydantic import JsonValue

type Json = JsonValue
type Obj = dict[str, JsonValue]

READ_CONTENT = "line one\nline two\n"
OTHER_CONTENT = "output\n"
PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_OUTPUT_MARKER = "\nOutput:\n"
_SESSION_META_KEYS = (
    "id",
    "session_id",
    "timestamp",
    "cwd",
    "runtime_workspace_roots",
    "originator",
    "cli_version",
    "source",
    "thread_source",
    "model_provider",
    "base_instructions",
    "history_mode",
    "history_base",
    "forked_from_id",
    "forked_from_ordinal_exclusive",
    "multi_agent_version",
    "context_window",
)
_TURN_CONTEXT_KEYS = (
    "turn_id",
    "root_turn_id",
    "cwd",
    "current_date",
    "approval_policy",
    "model",
    "summary",
    "effort",
)
_FIXTURES = ("script", "function", "interrupt", "compaction", "fork")


def _reads(lines: list[Obj]) -> dict[str, bool]:
    """Call id → whether its output is a file read. Script-mode items are
    assigned to the ``exec`` call they follow."""
    reads: dict[str, bool] = {}
    function_calls: set[str] = set()
    script_call: str | None = None
    for line in lines:
        match line:
            case {"payload": {"type": "function_call", "call_id": str(call_id)}}:
                function_calls.add(call_id)
            case {"payload": {"type": "custom_tool_call", "call_id": str(call_id)}}:
                script_call = call_id
            case {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "id": str(item_id),
                        "parsed_cmd": list(cmds),
                    },
                },
            }:
                is_read = bool(cmds) and all(
                    isinstance(c, dict) and c.get("type") == "read" for c in cmds
                )
                reads[item_id] = is_read
                if item_id not in function_calls and script_call is not None:
                    reads[script_call] = reads.get(script_call, False) or is_read
    return reads


@dataclass
class _Sanitiser:
    reads: dict[str, bool]
    prompts: dict[str, str] = field(default_factory=dict)
    answers: dict[str, str] = field(default_factory=dict)

    def line(self, line: Obj) -> Obj:
        payload = line["payload"]
        assert isinstance(payload, dict)
        match line["type"]:
            case "session_meta":
                payload = self._session_meta(payload)
            case "turn_context":
                payload = {k: payload[k] for k in _TURN_CONTEXT_KEYS if k in payload}
            case "response_item":
                payload = self._item(payload)
            case "event_msg":
                payload = self._event(payload)
            case "compacted":
                payload = self._compacted(payload)
            case "world_state":
                payload = {"full": True, "state": {}}
            case "token_usage_record":
                pass
            case _:
                payload = {}
        return {**line, "payload": payload}

    def _session_meta(self, payload: Obj) -> Obj:
        out = {k: v for k, v in payload.items() if k in _SESSION_META_KEYS}
        match payload.get("base_instructions"):
            case dict() as base:
                out["base_instructions"] = {**base, "text": "<instructions>"}
            case str():
                out["base_instructions"] = "<instructions>"
        return out

    def _item(self, item: Obj, *, window: int | None = None) -> Obj:
        match item:
            case {"type": "message", "role": "developer", "content": list(parts)}:
                return {**item, "content": [_text_part(p, "<developer>") for p in parts]}
            case {"type": "message", "role": "assistant", "content": list(parts)}:
                return {**item, "content": [self._answer_part(p) for p in parts]}
            case {"type": "message", "content": list(parts)}:
                return {**item, "content": [self._user_part(p) for p in parts]}
            case {"type": "reasoning"}:
                summary = item.get("summary")
                out: Obj = {k: v for k, v in item.items() if k != "content"}
                if isinstance(summary, list):
                    out["summary"] = [_text_part(s, "<reasoning>") for s in summary]
                if "encrypted_content" in out:
                    out["encrypted_content"] = "REDACTED"
                return out
            case {"type": "compaction"}:
                return {**item, "encrypted_content": f"COMPACTED-{window}"}
            case {
                "type": "function_call_output" | "custom_tool_call_output",
                "call_id": str(call_id),
                "output": output,
            }:
                return {**item, "output": self._output(output, self.reads.get(call_id, False))}
        return item

    def _event(self, event: Obj) -> Obj:
        match event:
            case {"type": "item_completed", "item": dict(item)}:
                return {**event, "item": self._codex_item(item)}
            case {"type": "task_complete", "last_agent_message": str(text)}:
                return {**event, "last_agent_message": self._answer(text)}
            case {"type": "token_count"}:
                return {**event, "rate_limits": None}
            case {"type": "task_started" | "task_complete" | "turn_aborted"}:
                return event
            case {"type": "thread_settings_applied", "thread_id": thread_id}:
                return {"type": "thread_settings_applied", "thread_id": thread_id}
        return {"type": event.get("type")}

    def _codex_item(self, item: Obj) -> Obj:
        match item:
            case {"type": "AgentMessage", "content": list(parts)}:
                return {**item, "content": [self._answer_part(p) for p in parts]}
            case {"type": "UserMessage", "content": list(parts)}:
                return {**item, "content": [self._user_part(p) for p in parts]}
            case {"type": "Reasoning"}:
                summary = item.get("summary_text")
                count = len(summary) if isinstance(summary, list) else 0
                return {**item, "summary_text": ["<reasoning>"] * count, "raw_content": []}
            case {"type": "CommandExecution", "id": str(item_id)}:
                body = READ_CONTENT if self.reads.get(item_id) else OTHER_CONTENT
                out = {**item}
                for key in ("stdout", "aggregated_output", "formatted_output"):
                    if key in out:
                        out[key] = body
                if out.get("stderr"):
                    out["stderr"] = "error\n"
                return out
        return item

    def _compacted(self, payload: Obj) -> Obj:
        window = payload.get("window_number")
        out = {**payload, "message": ""}
        history = payload.get("replacement_history")
        if isinstance(history, list):
            out["replacement_history"] = [
                self._item(i, window=window if isinstance(window, int) else None)
                if isinstance(i, dict)
                else i
                for i in history
            ]
        match payload.get("retained_context"):
            case {"user_messages": list(messages)} as retained:
                out["retained_context"] = {
                    **retained,
                    "verified_answers": [],
                    "user_messages": [
                        {**m, "text": self._prompt(m["text"])}
                        if isinstance(m, dict) and isinstance(m.get("text"), str)
                        else m
                        for m in messages
                    ],
                }
        return out

    def _output(self, output: Json, is_read: bool) -> Json:
        body = READ_CONTENT if is_read else OTHER_CONTENT
        if isinstance(output, str):
            head, sep, _ = output.partition(_OUTPUT_MARKER)
            return head + sep + body if sep else body
        if isinstance(output, list):
            return [_output_part(p, body) for p in output]
        return output

    def _user_part(self, part: Json) -> Json:
        if not isinstance(part, dict):
            return part
        text = part.get("text")
        if part.get("type") in ("input_text", "text") and isinstance(text, str):
            return {**part, "text": self._prompt(text)}
        if part.get("type") == "input_image":
            return {**part, "image_url": PNG_DATA_URL}
        return part

    def _answer_part(self, part: Json) -> Json:
        if isinstance(part, dict) and isinstance(text := part.get("text"), str):
            return {**part, "text": self._answer(text)}
        return part

    def _answer(self, text: str) -> str:
        return self.answers.setdefault(text, f"<answer {len(self.answers) + 1}>")

    def _prompt(self, text: str) -> str:
        if text.startswith("<environment_context>"):
            cwd = re.search(r"<cwd>.*?</cwd>", text)
            inner = f"  {cwd.group(0)}\n" if cwd else ""
            return f"<environment_context>\n{inner}</environment_context>"
        if text.startswith(("<turn_aborted>", "<image ", "</image>")):
            return text
        if text not in self.prompts:
            head, sep, request = text.partition("## My request:\n")
            neutral = f"Prompt {len(self.prompts) + 1}." + ("\n" if text.endswith("\n") else "")
            if sep and "# Files mentioned by the user:" in head:
                neutral = head + sep + (neutral if request.strip() else request)
            self.prompts[text] = neutral
        return self.prompts[text]


def _text_part(part: Json, text: str) -> Json:
    if isinstance(part, dict) and "text" in part:
        return {**part, "text": text}
    return part


def _output_part(part: Json, body: str) -> Json:
    if not isinstance(part, dict):
        return part
    if part.get("type") == "input_image":
        return {**part, "image_url": PNG_DATA_URL}
    text = part.get("text")
    if part.get("type") != "input_text" or not isinstance(text, str):
        return part
    if text.endswith("Output:\n"):
        return part
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict) and "output" in decoded:
        return {**part, "text": _dump({**decoded, "output": body})}
    return {**part, "text": body}


def _renamer(renames: list[tuple[str, str]]):
    pairs = sorted(
        {*renames, *((quote(a, safe="/"), quote(b, safe="/")) for a, b in renames)},
        key=lambda pair: -len(pair[0]),
    )

    def rename(value: Json) -> Json:
        if isinstance(value, str):
            for old, new in pairs:
                value = value.replace(old, new)
            return value
        if isinstance(value, list):
            return [rename(v) for v in value]
        if isinstance(value, dict):
            return {k: rename(v) for k, v in value.items()}
        return value

    return rename


def _dump(line: Json) -> str:
    return json.dumps(line, ensure_ascii=False, separators=(",", ":"))


def _rebase_fork(fork: list[Obj], parents: dict[str, list[Obj]]) -> None:
    """``history_base.end_byte_offset`` at the same ordinal of the sanitised parent."""
    match fork[0]:
        case {
            "payload": {
                "history_base": {
                    "thread_id": str(parent),
                    "end_ordinal_exclusive": int(end),
                } as base
            }
        }:
            offset = sum(len(_dump(line).encode()) + 1 for line in parents[parent][:end])
            base["end_byte_offset"] = offset


def _session_id(lines: list[Obj]) -> str:
    match lines[0]:
        case {"type": "session_meta", "payload": {"id": str(session_id)}}:
            return session_id
    raise SystemExit("first line is not a session_meta")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sanitise captured Codex rollouts.")
    for name in _FIXTURES:
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--rename", action="append", default=[], metavar="OLD=NEW")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "rollouts")
    args = parser.parse_args()

    home = str(Path.home())
    renames = [(home, "/home/user"), *(tuple(r.split("=", 1)) for r in args.rename)]
    rename = _renamer(renames)

    outputs: dict[str, list[Obj]] = {}
    for name in _FIXTURES:
        raw = [json.loads(line) for line in getattr(args, name).read_bytes().splitlines() if line]
        sanitiser = _Sanitiser(_reads(raw))
        outputs[name] = [rename(sanitiser.line(line)) for line in raw]

    _rebase_fork(outputs["fork"], {_session_id(lines): lines for lines in outputs.values()})

    forbidden = [Path(home).name.lower(), *(old.lower() for old, _ in renames[1:]), "gaaaaa"]
    args.out.mkdir(parents=True, exist_ok=True)
    for name, lines in outputs.items():
        text = "".join(_dump(line) + "\n" for line in lines)
        lowered = text.lower()
        if leaks := [word for word in forbidden if word in lowered]:
            raise SystemExit(f"{name}: still contains {leaks}")
        if set(re.findall(r"data:[^\"]+", text)) - {PNG_DATA_URL}:
            raise SystemExit(f"{name}: unexpected data URLs")
        (args.out / f"{name}.jsonl").write_text(text)


if __name__ == "__main__":
    main()
