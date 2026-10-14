"""Pieces shared by preflight invocations and rollout events."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from slashid_ai_forwarder_core.events import AIAccessedFile, AITool, AIToolServer, AIToolUse
from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedMessage

from .item_calls import failed
from .rollout import CodexItem, CommandExecution, FileChange, McpToolCall


def wire_time(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def failed_calls(items: Iterable[CodexItem]) -> set[str]:
    """Ids of calls that failed: commands, MCP calls and patches. An item's id is its
    call's: the real ``call_id`` in function mode, the synthesized call's in
    script mode."""
    return {
        item.id
        for item in items
        if (
            isinstance(item, CommandExecution)
            and (item.status == "failed" or item.exit_code not in (None, 0))
        )
        or (isinstance(item, McpToolCall | FileChange) and failed(item))
    }


def mark_errors(messages: Sequence[NormalizedMessage], failed: set[str]) -> list[NormalizedMessage]:
    """``messages`` with the results of ``failed`` calls flagged ``tool_is_error``."""
    if not failed:
        return list(messages)
    out: list[NormalizedMessage] = []
    for message in messages:
        if any(b.kind == "tool_result" and b.tool_use_id in failed for b in message.content):
            content = [
                b.model_copy(update={"tool_is_error": True})
                if b.kind == "tool_result" and b.tool_use_id in failed
                else b
                for b in message.content
            ]
            message = message.model_copy(update={"content": content})
        out.append(message)
    return out


def drop_shadowed_stubs(files: Iterable[AIAccessedFile]) -> list[AIAccessedFile]:
    """``files`` without the entries that have no hashes when another entry of the
    same name has some. A script's ``view_image`` result carries no image bytes (the
    item has only the path), so the extractor reports the path unhashed beside the
    preflight's hashed record. A connector file (it has a ``file_ref``) is merged on
    that ref instead, so it neither shadows nor is dropped."""
    files = list(files)
    hashed = {f.name for f in files if f.name and f.content_hashes and not f.file_ref}
    return [f for f in files if f.file_ref or f.content_hashes or f.name not in hashed]


def declared_from_calls(
    messages: Iterable[NormalizedMessage],
) -> tuple[list[AITool], list[AIToolServer]]:
    """Name-only declarations for every tool called in ``messages`` (the
    rollout records no definitions); ids equal ``resolve_tool``'s."""
    names = dict.fromkeys(
        block.tool_name
        for message in messages
        for block in message.content
        if block.kind == "tool_use" and block.tool_name and block.tool_executor != "server"
    )
    return build_tools_declared((name, None, None) for name in names)


def used_declarations(
    used: Sequence[AIToolUse], tools: Sequence[AITool], servers: Sequence[AIToolServer]
) -> tuple[list[AITool], list[AIToolServer]]:
    """The declared tools ``used`` refers to, and their servers."""
    ids = {u.tool_id for u in used}
    kept = [t for t in tools if t.id in ids]
    server_ids = {t.tool_server_id for t in kept}
    return kept, [s for s in servers if s.id in server_ids]
