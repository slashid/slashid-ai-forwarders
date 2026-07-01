"""MIL record → AIInvocationObservedV1 transformation.

Pure logic: no I/O. Builds AIInvocationObservedV1 envelopes for
POST /nhi/events/ai-invocations. Models mirror the SlashID OpenAPI
schemas (see `~/slashid/ng-evangelion/spec/openapi.yaml`, components
AIInvocationObservedV1 et al). `model_dump(mode="json", exclude_none=True)`
produces wire-compatible payloads.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Enum mirrors of the OpenAPI schema. Kept as Literal so pydantic both
# accepts and rejects values without dragging in a runtime Enum class.
AIToolServerKind = Literal["mcp", "runtime"]

AIStopReason = Literal[
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "guardrail_intervened",
    "content_filtered",
    "malformed_model_output",
    "malformed_tool_use",
    "model_context_window_exceeded",
    "unknown",
]


class _WireModel(BaseModel):
    """Base for outbound wire-format models."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AIInvocationTokens(_WireModel):
    """spec/openapi.yaml — AIInvocationTokens. All fields required, int64."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0


class AIModel(_WireModel):
    """spec/openapi.yaml — AIModelDetails."""

    id: str
    name: str | None = None
    provider: str | None = None
    family: str | None = None
    version: str | None = None
    raw_model_id: str | None = None


class AIToolAnnotations(_WireModel):
    """spec/openapi.yaml — AIToolAnnotations. All MCP-style hints, all optional."""

    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None


class AITool(_WireModel):
    """spec/openapi.yaml — AIToolDetails. Only `id` is required server-side."""

    id: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    input_schema: str | None = None
    output_schema: str | None = None
    annotations: AIToolAnnotations | None = None
    tool_server_id: str | None = None


class AIToolServerCapabilities(_WireModel):
    """spec/openapi.yaml — AIToolServerCapabilities."""

    supports_prompts: bool | None = None
    supports_resources: bool | None = None
    supports_tools: bool | None = None
    supports_logging: bool | None = None
    supports_completions: bool | None = None
    prompts_list_changed: bool | None = None
    resources_list_changed: bool | None = None
    tools_list_changed: bool | None = None
    resources_subscribe: bool | None = None


class AIToolServer(_WireModel):
    """spec/openapi.yaml — AIToolServerDetails."""

    id: str
    name: str | None = None
    kind: AIToolServerKind | None = None
    title: str | None = None
    version: str | None = None
    instructions: str | None = None
    capabilities: AIToolServerCapabilities | None = None


class AWSIdentityDetails(_WireModel):
    """AWS-source shape of AIInvocationObservedV1.identity_details.

    `principal_arn` identifies the caller; `access_key_id` enables the
    server's AssumeRole-chain unrolling when set.
    """

    principal_arn: str
    access_key_id: str | None = None


class AIAgentDetails(_WireModel):
    """spec/openapi.yaml — AIAgentDetails.

    Mirrors the tool/server pattern but for sub-agents. Bedrock MIL doesn't
    expose this today; included for schema completeness.
    """

    id: str
    name: str | None = None
    provider: str | None = None
    title: str | None = None
    description: str | None = None
    version: str | None = None
    raw_agent_id: str | None = None


class AIInvocationContent(_WireModel):
    """spec/openapi.yaml — AIInvocationContent.

    Carries the input or output body. `content_hash`, `mime_type`, and
    `byte_length` are non-sensitive and always populated; `redacted_text`
    only when the customer opts in via SLASHID_INCLUDE_RAW_CONTENT.
    """

    redacted_text: str | None = None
    content_hash: str | None = None
    mime_type: str | None = None
    byte_length: int | None = None


class AIInvocationObservedV1(_WireModel):
    """spec/openapi.yaml — AIInvocationObservedV1. Body for POST /nhi/events/ai-invocations.

    `org_id`/`connection_id` aren't sent — server derives them from the
    authenticated push token. Schema dropped them as required fields too.
    """

    request_id: str
    timestamp: str
    identity_details: AWSIdentityDetails
    model: AIModel
    tokens: AIInvocationTokens = Field(default_factory=AIInvocationTokens)
    available_agents: list[AIAgentDetails] | None = None
    used_agent_ids: list[str] | None = None
    available_tool_servers: list[AIToolServer] | None = None
    available_tools: list[AITool] | None = None
    used_tool_ids: list[str] | None = None
    stop_reason: AIStopReason | None = None
    conversation_id: str | None = None
    input: AIInvocationContent | None = None
    output: AIInvocationContent | None = None


# --- record parsing ---------------------------------------------------------

# Bedrock / Anthropic stop reasons map 1:1 onto the AIStopReason enum once we
# fall back to "unknown" for anything not in the union.
_STOP_REASON_VALUES: frozenset[str] = frozenset(
    [
        "end_turn",
        "max_tokens",
        "stop_sequence",
        "tool_use",
        "pause_turn",
        "refusal",
        "guardrail_intervened",
        "content_filtered",
        "malformed_model_output",
        "malformed_tool_use",
        "model_context_window_exceeded",
        "unknown",
    ]
)


def _ts(record: dict[str, Any]) -> str:
    raw = record.get("timestamp") or record.get("eventTime") or ""
    if not raw:
        return datetime.now(tz=UTC).isoformat()
    if isinstance(raw, str) and raw.endswith("Z"):
        return raw[:-1] + "+00:00"
    return str(raw)


def _identity_details(record: dict[str, Any]) -> AWSIdentityDetails | None:
    """Build identity_details from MIL's `identity` block.

    MIL gives us the assumed-role ARN directly; we forward it raw and let
    the server-side AssumeRole unroller resolve it to a human IAM user via
    `access_key_id` when present.

    Returns None when neither `arn` nor `resolved_arn` is set — callers
    should drop the event rather than ship `principal_arn = ""` and let
    the server reject (or worse, accept) bad-data placeholders.
    """
    ident = record.get("identity") or {}
    principal = ident.get("resolved_arn") or ident.get("arn") or ""
    if not principal:
        return None
    access_key = ident.get("accessKeyId") or None
    return AWSIdentityDetails(principal_arn=principal, access_key_id=access_key)


def _build_content(body: Any, *, include_text: bool) -> AIInvocationContent | None:
    """Hash + size + (optionally) raw text for an inputBodyJson / outputBodyJson.

    The body is serialised canonically so the hash is stable across runs
    regardless of dict-key ordering. `redacted_text` only gets set when
    the caller has opted in — otherwise we send hash / mime / byte length,
    which carry no prompt content but still let SlashID dedup + correlate.
    """
    if not isinstance(body, dict | list) or not body:
        return None
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return AIInvocationContent(
        content_hash=f"sha256:{hashlib.sha256(serialized).hexdigest()}",
        mime_type="application/json",
        byte_length=len(serialized),
        redacted_text=serialized.decode() if include_text else None,
    )


def _stop_reason(record: dict[str, Any]) -> AIStopReason | None:
    obody = (record.get("output") or {}).get("outputBodyJson")
    # Non-Anthropic streams reach us as raw lists (see mil_normalize.py — we
    # only normalize shapes we own). Skip cleanly instead of crashing on .get().
    if not isinstance(obody, dict):
        return None
    raw = obody.get("stopReason")
    if not isinstance(raw, str) or not raw:
        return None
    if raw in _STOP_REASON_VALUES:
        # ty/pydantic narrows the union for us once `raw` is in the known set.
        return raw  # type: ignore[return-value]
    return "unknown"


def parse_tool_name(name: str) -> tuple[str, str, AIToolServerKind]:
    """Split a Bedrock toolSpec name into (tool_name, server_name, server_kind).

    - `mcp__{server}__{tool}` → (`{tool}`, `{server}`, `"mcp"`)
    - `{server}__{tool}`      → (`{tool}`, `{server}`, `"runtime"`)
    - `{tool}`                → (`{tool}`, `"builtin"`, `"runtime"`)
      (bare tools share one synthetic `builtin` server; `runtime` is the
       AIToolServerDetails.kind enum value the SlashID schema uses for
       any non-MCP server.)
    """
    if name.startswith("mcp__"):
        rest = name[len("mcp__") :]
        if "__" in rest:
            server, tool = rest.split("__", 1)
            return tool, server, "mcp"
        return rest, "builtin", "runtime"
    if "__" in name:
        server, tool = name.split("__", 1)
        return tool, server, "runtime"
    return name, "builtin", "runtime"


def _short_hash(obj: str | dict[str, object]) -> str:
    if isinstance(obj, str):
        data = obj.encode()
    else:
        data = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()[:16]


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
        description = spec.get("description") or None

        # Server ID: name + kind (servers carry no other distinguishing data).
        server_id = _short_hash({"name": server_name, "kind": server_kind})

        # Tool ID: full canonical spec — same name with different description,
        # schema, or annotations is a different tool.
        tool_id = _short_hash(
            {
                "server": server_name,
                "name": tool_name,
                "description": description,
                "input_schema": schema or None,
            }
        )

        if server_id not in servers_by_id:
            servers_by_id[server_id] = AIToolServer(
                id=server_id,
                name=server_name,
                kind=server_kind,
            )

        tools.append(
            AITool(
                id=tool_id,
                name=tool_name,
                tool_server_id=server_id,
                description=description,
                input_schema=json.dumps(schema, separators=(",", ":")) if schema else None,
            )
        )
        raw_to_id[raw_name] = tool_id

    return list(servers_by_id.values()), tools, raw_to_id


def _used_tool_ids(record: dict[str, Any], raw_name_to_id: dict[str, str]) -> list[str]:
    """Extract tool IDs from the assistant response's `toolUse` blocks."""
    obody = (record.get("output") or {}).get("outputBodyJson")
    # Non-Anthropic streams reach us as raw lists (see mil_normalize.py — we
    # only normalize shapes we own). Skip cleanly instead of crashing on .get().
    if not isinstance(obody, dict):
        return []
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
    include_raw_content: bool = False,
    model_region: str | None = None,
) -> AIInvocationObservedV1 | None:
    """Build the AIInvocationObservedV1 for a single MIL record.

    Returns None when the record lacks a `requestId` (body-offload S3
    objects share the listing prefix in some MIL layouts; they appear as
    pseudo-records with no identifying metadata).

    `include_raw_content` defaults to off — by default we send hash, mime,
    and byte length on `input`/`output` but no prompt text. Flip via the
    SLASHID_INCLUDE_RAW_CONTENT env var (CFN parameter same name).
    """
    if not record.get("requestId"):
        return None

    identity = _identity_details(record)
    if identity is None:
        # No usable principal ARN — server would reject identity_details
        # anyway, and a placeholder would pollute the AI subgraph.
        return None

    servers, tools, raw_to_id = _available_tools(record)
    used = _used_tool_ids(record, raw_to_id)

    inp = record.get("input") or {}
    out = record.get("output") or {}
    raw_model_id = str(record.get("modelId") or "")

    region = model_region or str(record.get("region") or "")
    model_info = None
    if raw_model_id and region:
        from .model_catalog import get_model_info

        model_info = get_model_info(raw_model_id, region)

    # model.id: use raw when it's already an ARN, else catalog ARN, else raw
    model_id = (
        raw_model_id
        if raw_model_id.startswith("arn:")
        else (model_info["arn"] if model_info else raw_model_id)
    )

    return AIInvocationObservedV1(
        request_id=str(record["requestId"]),
        timestamp=_ts(record),
        identity_details=identity,
        model=AIModel(
            id=model_id,
            name=model_info["name"] if model_info else None,
            provider=model_info["provider"] if model_info else None,
            raw_model_id=raw_model_id or None,
        ),
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
        input=_build_content(inp.get("inputBodyJson"), include_text=include_raw_content),
        output=_build_content(out.get("outputBodyJson"), include_text=include_raw_content),
    )
