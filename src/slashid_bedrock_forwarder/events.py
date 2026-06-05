"""MIL record → AIInvocationObservedV1 transformation.

Pure logic: no I/O. Builds AIInvocationObservedV1 envelopes for
POST /nhi/events/ai-invocations. Models mirror the SlashID OpenAPI
schemas; `model_dump(mode="json", exclude_none=True)` produces
wire-compatible payloads.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _WireModel(BaseModel):
    """Base for outbound wire-format models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AIInvocationTokens(_WireModel):
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0


class AIToolServer(_WireModel):
    id: str
    name: str
    kind: str


class AITool(_WireModel):
    id: str
    name: str
    tool_server_id: str
    description: str | None = None
    input_schema: str | None = None


class AIModel(_WireModel):
    id: str


class AIInvocationObservedV1(_WireModel):
    """Body for POST /nhi/events/ai-invocations (single event)."""

    org_id: str
    connection_id: str
    request_id: str
    timestamp: str
    identifier_from_source: str
    identity_source_type: str
    model: AIModel
    tokens: AIInvocationTokens = Field(default_factory=AIInvocationTokens)
    available_tool_servers: list[AIToolServer] | None = None
    available_tools: list[AITool] | None = None
    used_tool_ids: list[str] | None = None
    stop_reason: str | None = None


_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "guardrail_intervened": "guardrail_intervened",
    "content_filtered": "content_filtered",
    "pause_turn": "pause_turn",
}


def _ts(record: dict[str, Any]) -> str:
    raw = record.get("timestamp") or record.get("eventTime") or ""
    if not raw:
        return datetime.now(tz=UTC).isoformat()
    if isinstance(raw, str) and raw.endswith("Z"):
        return raw[:-1] + "+00:00"
    return str(raw)


def _identifier(record: dict[str, Any]) -> str:
    ident = record.get("identity") or {}
    return ident.get("resolved_arn") or ident.get("arn") or ""


def _stop_reason(record: dict[str, Any]) -> str | None:
    obody = (record.get("output") or {}).get("outputBodyJson") or {}
    raw = obody.get("stopReason")
    if isinstance(raw, str) and raw:
        return _STOP_REASON_MAP.get(raw, "unknown")
    return None


def parse_tool_name(name: str) -> tuple[str, str, str]:
    """Split a Bedrock toolSpec name into (tool_name, server_name, server_kind).

    - `mcp__{server}__{tool}` → (`{tool}`, `{server}`, `mcp`)
    - `{server}__{tool}`      → (`{tool}`, `{server}`, `builtin`)
    - `{tool}`                → (`{tool}`, `builtin`,  `builtin`)
      (bare tools share one synthetic `builtin` server.)
    """
    if name.startswith("mcp__"):
        rest = name[len("mcp__") :]
        if "__" in rest:
            server, tool = rest.split("__", 1)
            return tool, server, "mcp"
        return rest, "builtin", "builtin"
    if "__" in name:
        server, tool = name.split("__", 1)
        return tool, server, "builtin"
    return name, "builtin", "builtin"


def _short_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _available_tools(
    record: dict[str, Any],
) -> tuple[list[AIToolServer], list[AITool], dict[str, str]]:
    """Return (tool_servers, tools, raw_name_to_tool_id) from the record's toolConfig."""
    body = (record.get("input") or {}).get("inputBodyJson") or {}
    tool_config = body.get("toolConfig") or {}
    raw_tools = tool_config.get("tools") or []

    servers_by_id: dict[str, AIToolServer] = {}
    tools: list[AITool] = []
    raw_to_id: dict[str, str] = {}

    for entry in raw_tools:
        spec = (entry or {}).get("toolSpec") or {}
        raw_name = spec.get("name")
        if not raw_name:
            continue

        tool_name, server_name, server_kind = parse_tool_name(raw_name)
        schema = (spec.get("inputSchema") or {}).get("json") or {}
        description = spec.get("description")

        server_id = _short_hash(server_name)
        tool_id = _short_hash(f"{server_name}__{tool_name}")

        if server_id not in servers_by_id:
            servers_by_id[server_id] = AIToolServer(
                id=server_id, name=server_name, kind=server_kind
            )

        tools.append(
            AITool(
                id=tool_id,
                name=tool_name,
                tool_server_id=server_id,
                description=description if description else None,
                input_schema=json.dumps(schema, separators=(",", ":")) if schema else None,
            )
        )
        raw_to_id[raw_name] = tool_id

    return list(servers_by_id.values()), tools, raw_to_id


def _used_tool_ids(record: dict[str, Any], raw_name_to_id: dict[str, str]) -> list[str]:
    """Extract tool IDs from the assistant response's `toolUse` blocks."""
    obody = (record.get("output") or {}).get("outputBodyJson") or {}
    message = (obody.get("output") or {}).get("message") or {}
    ids: list[str] = []
    seen: set[str] = set()
    for block in message.get("content", []) or []:
        tu = (block or {}).get("toolUse") or {}
        name = tu.get("name")
        if not name:
            continue
        tool_id = raw_name_to_id.get(name)
        if tool_id and tool_id not in seen:
            ids.append(tool_id)
            seen.add(tool_id)
    return ids


def build_event(
    record: dict[str, Any],
    *,
    org_id: str,
    connection_id: str,
    identity_source_type: str,
) -> AIInvocationObservedV1 | None:
    """Build the AIInvocationObservedV1 for a single MIL record.

    Returns None when the record lacks a `requestId` (body-offload S3
    objects share the listing prefix in some MIL layouts; they appear as
    pseudo-records with no identifying metadata).
    """
    if not record.get("requestId"):
        return None

    servers, tools, raw_to_id = _available_tools(record)
    used = _used_tool_ids(record, raw_to_id)

    inp = record.get("input") or {}
    out = record.get("output") or {}

    return AIInvocationObservedV1(
        org_id=org_id,
        connection_id=connection_id,
        request_id=str(record["requestId"]),
        timestamp=_ts(record),
        identifier_from_source=_identifier(record),
        identity_source_type=identity_source_type,
        model=AIModel(id=str(record.get("modelId") or "")),
        tokens=AIInvocationTokens(
            input=int(inp.get("inputTokenCount") or 0),
            output=int(out.get("outputTokenCount") or 0),
            cache_read=int(inp.get("cacheReadInputTokenCount") or 0),
            cache_write=int(inp.get("cacheWriteInputTokenCount") or 0),
            reasoning=0,
        ),
        available_tool_servers=servers or None,
        available_tools=tools or None,
        used_tool_ids=used or None,
        stop_reason=_stop_reason(record),
    )
