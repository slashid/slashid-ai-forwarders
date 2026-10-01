from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.events import AIAccessedFile, AIInvocationObservedV1
from slashid_ai_forwarder_core.files import hash_local_file
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool

from slashid_codex.config import CodexConfig
from slashid_codex.cursor import RolloutCursor, RolloutInvocation
from slashid_codex.dev_platform import DevPlatform
from slashid_codex.events import SessionContext, build_event
from slashid_codex.log import SessionLog
from slashid_codex.mcp_servers import parse_servers
from slashid_codex.state import SqliteFileRecordStore

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
FIXTURES = Path(__file__).parent / "fixtures"
READ_CONTENT = "line one\nline two\n"
BASH = resolve_tool("Bash")


def _invocations(name: str) -> tuple[list[RolloutInvocation], RolloutCursor]:
    log = SessionLog.open(ROLLOUTS / f"{name}.jsonl", lambda _: None)
    log.refresh()
    cursor = RolloutCursor(log)
    out = []
    while (invocation := cursor.next_closed()) is not None:
        out.append(invocation)
    return out, cursor


class Builder:
    def __init__(self, tmp_path: Path, config: CodexConfig) -> None:
        self.config = config
        self.records = SqliteFileRecordStore(DevPlatform(tmp_path / "state").connect)

    async def events(
        self, name: str, session_id: str = "sess", **context: object
    ) -> list[AIInvocationObservedV1]:
        invocations, cursor = _invocations(name)
        ctx = SessionContext(
            session_id,
            originator=cursor.originator,
            cli_version=cursor.cli_version,
            **context,  # ty: ignore[invalid-argument-type]
        )
        return [
            await build_event(i, ctx, config=self.config, records=self.records) for i in invocations
        ]


@pytest.fixture
def builder(tmp_path: Path, make_config: Callable[..., CodexConfig]) -> Builder:
    return Builder(tmp_path, make_config())


def _file(tmp_path: Path, name: str, content: str) -> AIAccessedFile:
    path = tmp_path / "f"
    path.write_text(content)
    entry = hash_local_file(path, max_bytes=1 << 20, provenance="tool_result")
    return entry.model_copy(update={"name": name})


async def test_script_mode(builder: Builder) -> None:
    invocations, _ = _invocations("script")
    first, second = await builder.events("script")
    assert [first.request_id, second.request_id] == [i.response_id for i in invocations]
    assert first.timestamp == "2026-09-30T18:33:45.307Z"
    assert first.parsed_as == "codex-rollout"
    assert first.user_agent == "codex_exec/0.158.0-alpha.2.1"
    assert first.conversation_id == "sess"
    assert first.identity_details.model_dump(exclude_none=True) == {
        "kind": "openai",
        "user_id": "user-abc",
    }
    assert first.model.id == "gpt-6-astra"
    assert first.model.provider == "openai"
    assert first.tokens.cache_write == 15186
    assert first.tokens.input == 3
    assert first.used_tools is None
    assert first.requested_tool_uses is not None
    assert [u.tool_id for u in first.requested_tool_uses] == [BASH[0].id]
    assert second.used_tools is not None
    [used] = second.used_tools
    assert used.tool_id == BASH[0].id
    assert used.is_error is False
    assert second.available_tools == [BASH[0]]


async def test_function_mode(builder: Builder, tmp_path: Path) -> None:
    invocations, _ = _invocations("function")
    [sed_item] = invocations[2].consumed_items
    assert sed_item.id is not None
    record = _file(tmp_path, "/home/user/Recipes/notes.md", READ_CONTENT)
    builder.records.put_call("sess", invocations[1].turn_id, sed_item.id, record)
    attachment = _file(tmp_path, "/home/user/Downloads/notes.md", "a").model_copy(
        update={"provenance": "attachment"}
    )
    builder.records.put_turn("sess", invocations[0].turn_id, [attachment])

    events = await builder.events("function")

    assert events[0].accessed_files == [attachment]
    requested = events[1].requested_tool_uses
    assert requested is not None
    assert [(u.tool_id, u.tool_use_id) for u in requested] == [(BASH[0].id, sed_item.id)]
    # The whole-file read's tool-result entry dedupes against its record.
    assert events[2].accessed_files == [record]
    assert events[1].accessed_files is None


async def test_failed_command_is_error(builder: Builder) -> None:
    invocations, _ = _invocations("function")
    [sed_item] = invocations[2].consumed_items
    failed = sed_item.model_copy(update={"exit_code": 1})
    patched = invocations[2].model_copy(update={"consumed_items": (failed,)})
    event = await build_event(
        patched, SessionContext("sess"), config=builder.config, records=builder.records
    )
    assert event.used_tools is not None
    assert [u.is_error for u in event.used_tools] == [True]


async def test_mcp_servers_listed(builder: Builder) -> None:
    servers = parse_servers((FIXTURES / "mcp_list.json").read_bytes())
    [first, _] = await builder.events("script", mcp_servers=tuple(servers))
    assert first.available_tool_servers is not None
    assert [s.name for s in first.available_tool_servers] == [
        "builtin",
        "node_repl",
        "cua_repl",
        "docs",
    ]


async def test_compaction_round_links(builder: Builder) -> None:
    events = await builder.events("compaction")
    assert [e.parsed_as for e in events] == [
        "codex-rollout",
        "codex-rollout",
        "codex-compaction",
        "codex-rollout",
        "codex-rollout",
    ]
    compaction = events[2]
    assert compaction.round_hash is not None
    after = events[3]
    assert after.recent_round_hashes == [
        after.round_hash,
        compaction.round_hash,
        events[1].round_hash,
        events[0].round_hash,
        "start",
    ]


async def test_history_truncated(builder: Builder) -> None:
    [first, _] = await builder.events("script", history_truncated=True)
    assert first.recent_round_hashes is not None
    assert first.recent_round_hashes[-1] == "..."
