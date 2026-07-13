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
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .content_utils import strip_cat_n, truncate_middle

log = logging.getLogger(__name__)


class _ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    field_name: str
    cleanup: Callable[[str], str] | None = None


# Canonical reference: https://docs.anthropic.com/en/docs/claude-code/tools
_READ_TOOLS: dict[str, _ToolSpec] = {
    "Read": _ToolSpec(field_name="file_path", cleanup=strip_cat_n),  # Claude Code (cat-n output)
    "ReadFile": _ToolSpec(field_name="path"),  # OpenCode, Amazon Q Developer, Gemini CLI
    "read_file": _ToolSpec(field_name="path"),  # snake_case variants
    "view_file": _ToolSpec(field_name="path"),  # some agents
    "str_replace_based_edit_tool": _ToolSpec(
        field_name="path"
    ),  # Claude computer-use text editor view
}


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


class AIToolUse(_WireModel):
    """One completed tool invocation within an AI turn.

    Only emitted once the tool has actually run and the client has returned
    the result — pairing an assistant `tool_use` block with the matching
    `tool_result`. In-flight calls (a `tool_use` in this record's output
    with no result yet) are deferred: they'll appear on the invocation
    event that carries the result.

    `trace_id` is the OTel trace id echoed by MCP servers via a
    `$opentelemetry` block in `structuredContent` (see mcp-gate-demo
    CorrelationIdMiddleware) — it correlates this tool call with the
    corresponding Gate observability event on the SlashID side. Absent
    when the MCP server doesn't run the middleware.
    """

    tool_id: str
    is_error: bool
    trace_id: str | None = None


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

    Carries the input or output body. `content_hashes`, `mime_type`, and
    `byte_length` are non-sensitive and always populated; `redacted_text`
    only when the customer opts in via SLASHID_INCLUDE_RAW_CONTENT.
    """

    redacted_text: str | None = None
    content_hashes: dict[str, str] | None = None
    mime_type: str | None = None
    byte_length: int | None = None


class AIAccessedFile(_WireModel):
    """spec/openapi.yaml — AIAccessedFile."""

    name: str | None = None
    content_hashes: dict[str, str] | None = None
    media_type: str | None = None
    byte_length: int | None = None
    redacted_content: str | None = None


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
    used_tools: list[AIToolUse] | None = None
    stop_reason: AIStopReason | None = None
    conversation_id: str | None = None
    input: AIInvocationContent | None = None
    output: AIInvocationContent | None = None
    accessed_files: list[AIAccessedFile] | None = None


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


def _build_content(
    body: Any, *, include_text: bool, max_content_size: int
) -> AIInvocationContent | None:
    """Hash + size + (optionally) raw text for an inputBodyJson / outputBodyJson.

    The body is serialised canonically so the hash is stable across runs
    regardless of dict-key ordering. `redacted_text` only gets set when
    the caller has opted in — otherwise we send hash / mime / byte length,
    which carry no prompt content but still let SlashID dedup + correlate.
    """
    if not isinstance(body, dict | list) or not body:
        return None
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    redacted_text: str | None = None
    if include_text:
        redacted_text = truncate_middle(serialized.decode(), max_content_size)
    return AIInvocationContent(
        content_hashes={
            "sha256": hashlib.sha256(serialized).hexdigest(),
            "sha1": hashlib.sha1(serialized).hexdigest(),
            "md5": hashlib.md5(serialized).hexdigest(),
        },
        mime_type="application/json",
        byte_length=len(serialized),
        redacted_text=redacted_text,
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
    if not isinstance(body, dict):
        return [], [], {}
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


async def _accessed_files(
    record: dict[str, Any],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    """Extract document and image attachments from Converse-shape input messages.

    Converse document block:
      messages[].content[]{document:{name, format, source:{bytes:<base64>}}}
    Converse image block:
      messages[].content[]{image:{format, source:{bytes:<base64>}}}
      (images carry no name)

    Only considers messages after the last assistant message — files in
    earlier turns were already reported in prior invocations.

    S3-sourced attachments are resolved inline via HeadObject + optional
    GetObject (gated by max_fetch_bytes). Files are deduplicated by
    (name, content_hash).
    """
    import asyncio
    import base64 as _b64
    import mimetypes

    from .s3 import MAX_PARALLEL_FETCHES, _resolve_s3_attachment

    def _mime_from_name(name: str | None) -> str | None:
        if not name:
            return None
        mt, _ = mimetypes.guess_type(name)
        return mt or None

    # Bedrock Converse format enum → IANA media types.
    # Document canonical list: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_DocumentBlock.html
    # Image canonical list:    https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ImageBlock.html
    _DOC_MIME: dict[str, str] = {
        # document formats
        "pdf": "application/pdf",
        "csv": "text/csv",
        "txt": "text/plain",
        "md": "text/markdown",
        "html": "text/html",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        # image formats
        "png": "image/png",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }

    body = (record.get("input") or {}).get("inputBodyJson")
    if not isinstance(body, dict):
        return []
    files: list[AIAccessedFile] = []
    seen: set[tuple[str | None, str | None]] = set()

    def _decode_b64(val: Any) -> bytes | None:
        if not val:
            return None
        try:
            return _b64.b64decode(val)
        except Exception:
            return None

    def _add(
        name: str | None,
        media_type: str | None,
        raw_bytes: bytes | None,
        length: int | None = None,
        partial_head: bytes | None = None,
        partial_tail: bytes | None = None,
    ) -> None:
        # Full bytes → stable hashes. Partial fetch → no hashes (bytes are incomplete).
        if raw_bytes is not None:
            content_hashes: dict[str, str] | None = {
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "sha1": hashlib.sha1(raw_bytes).hexdigest(),
                "md5": hashlib.md5(raw_bytes).hexdigest(),
            }
        else:
            content_hashes = None
        key = (name, content_hashes["sha256"] if content_hashes else None)
        if key in seen:
            return
        seen.add(key)
        if include_raw_content:
            if raw_bytes is not None:
                redacted = truncate_middle(raw_bytes.decode(errors="replace"), max_content_size)
            elif partial_head is not None and partial_tail is not None:
                head_str = partial_head.decode(errors="replace")
                tail_str = partial_tail.decode(errors="replace")
                combined = head_str + "…" + tail_str
                redacted = truncate_middle(combined, max_content_size)
            else:
                redacted = None
        else:
            redacted = None
        files.append(
            AIAccessedFile(
                name=name,
                content_hashes=content_hashes,
                media_type=media_type,
                byte_length=len(raw_bytes) if raw_bytes is not None else length,
                redacted_content=redacted,
            )
        )

    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1,
    )

    # Collect all S3 source blocks from the current turn so we can resolve
    # them concurrently before building the file list.
    s3_sources: list[dict[str, Any]] = []
    for msg in messages[last_assistant + 1 :]:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            for key in ("document", "image"):
                item = block.get(key)
                if isinstance(item, dict):
                    src = item.get("source") or {}
                    if "s3Location" in src or "s3Uri" in src:
                        s3_sources.append(src)

    if s3_sources:
        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

        async def _guarded(src: dict[str, Any]) -> None:
            async with sem:
                await _resolve_s3_attachment(src, max_content_size=max_content_size)

        await asyncio.gather(*(_guarded(src) for src in s3_sources))

    for msg in messages[last_assistant + 1 :]:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue

            if "document" in block:
                doc = block["document"] or {}
                fmt = doc.get("format") or None
                source = doc.get("source") or {}
                name = doc.get("name") or None
                if "bytes" in source:
                    raw_bytes = _decode_b64(source["bytes"])
                    _add(
                        name=name,
                        media_type=(
                            _DOC_MIME.get(fmt, f"application/{fmt}")
                            if fmt
                            else _mime_from_name(name)
                        ),
                        raw_bytes=raw_bytes,
                    )
                else:
                    uri = (source.get("s3Location") or {}).get("uri") or source.get("s3Uri") or None
                    file_name = name or uri
                    # Guess from URI when name has no extension (e.g. "report" vs "report.pdf").
                    mime_hint = _mime_from_name(file_name) or _mime_from_name(uri)
                    if "_resolved_byte_length" not in source:
                        # HEAD failed (permissions, object missing, etc.) — emit
                        # a stub so callers know the file was referenced.
                        _add(
                            name=file_name,
                            media_type=mime_hint,
                            raw_bytes=None,
                        )
                    else:
                        # fmt → map → HeadObject ContentType → filename guess
                        media_type = (
                            _DOC_MIME.get(fmt, f"application/{fmt}")
                            if fmt
                            else source.get("_resolved_content_type") or mime_hint
                        )
                        _add(
                            name=file_name,
                            media_type=media_type,
                            raw_bytes=source.get("_resolved_bytes"),
                            length=source.get("_resolved_byte_length"),
                            partial_head=source.get("_resolved_head_bytes"),
                            partial_tail=source.get("_resolved_tail_bytes"),
                        )

            elif "image" in block:
                img = block["image"] or {}
                fmt = img.get("format") or None
                source = img.get("source") or {}
                if "bytes" in source:
                    raw_bytes = _decode_b64(source["bytes"])
                    _add(
                        name=None,
                        media_type=_DOC_MIME.get(fmt, f"image/{fmt}") if fmt else None,
                        raw_bytes=raw_bytes,
                    )
                else:
                    uri = (source.get("s3Location") or {}).get("uri") or source.get("s3Uri") or None
                    if "_resolved_byte_length" not in source:
                        _add(
                            name=uri,
                            media_type=_mime_from_name(uri),
                            raw_bytes=None,
                        )
                    else:
                        media_type = (
                            _DOC_MIME.get(fmt, f"image/{fmt}")
                            if fmt
                            else source.get("_resolved_content_type") or _mime_from_name(uri)
                        )
                        _add(
                            name=uri,
                            media_type=media_type,
                            raw_bytes=source.get("_resolved_bytes"),
                            length=source.get("_resolved_byte_length"),
                            partial_head=source.get("_resolved_head_bytes"),
                            partial_tail=source.get("_resolved_tail_bytes"),
                        )

    # --- tool-result files ---------------------------------------------------

    # Build a lookup of tool_use_id → {name, input} from all assistant messages.
    tool_use_by_id: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            # Anthropic shape: {type: "tool_use", id, name, input}
            # Converse shape:  {toolUse: {toolUseId, name, input}}
            if block.get("type") == "tool_use":
                uid = block.get("id")
                if uid:
                    tool_use_by_id[uid] = {
                        "name": block.get("name"),
                        "input": block.get("input") or {},
                    }
            elif "toolUse" in block:
                tu = block["toolUse"] or {}
                uid = tu.get("toolUseId")
                if uid:
                    tool_use_by_id[uid] = {
                        "name": tu.get("name"),
                        "input": tu.get("input") or {},
                    }

    for msg in messages[last_assistant + 1 :]:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            # Anthropic shape: {type: "tool_result", tool_use_id, content}
            # Converse shape:  {toolResult: {toolUseId, content}}
            if block.get("type") == "tool_result":
                uid = block.get("tool_use_id")
                raw_content = block.get("content")
            elif "toolResult" in block:
                tr = block["toolResult"] or {}
                uid = tr.get("toolUseId")
                raw_content = tr.get("content")
            else:
                continue

            tu = tool_use_by_id.get(uid or "")
            if not tu:
                continue
            spec = _READ_TOOLS.get(tu.get("name") or "")
            if not spec:
                continue

            path = (tu["input"] or {}).get(spec.field_name) or None
            if not path:
                continue

            def _apply_cleanup(text: str, _spec: _ToolSpec = spec) -> str:
                return _spec.cleanup(text) if _spec.cleanup else text

            # Hash the returned content when available.
            content_bytes: bytes | None = None
            if isinstance(raw_content, str):
                content_bytes = _apply_cleanup(raw_content).encode()
            elif isinstance(raw_content, list):
                # Converse content array — concatenate text blocks
                text = "".join(b.get("text", "") for b in raw_content if isinstance(b, dict))
                if text:
                    content_bytes = _apply_cleanup(text).encode()

            _add(
                name=path,
                media_type=_mime_from_name(path),
                raw_bytes=content_bytes,
                length=len(content_bytes) if content_bytes is not None else None,
            )

    return files


# $opentelemetry: the OTel context that MCP servers echo back on tool results
# (see mcp-gate-demo CorrelationIdMiddleware). Wire shape:
#   {"$opentelemetry": {"trace_id": "<32 hex>", "span_id": "<16 hex>"}}
# Only lives in MCP `structuredContent`. Reaches Bedrock Converse either as a
# native `{json: {...}}` block or, when the client stringifies structured
# content, as a JSON-encoded `{text: "..."}` block. We handle both.
_OTEL_KEY = "$opentelemetry"
_TRACE_ID_HEX = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def _trace_id_from_dict(d: Any) -> str | None:
    """Pull `$opentelemetry.trace_id` out of a decoded structured-content dict."""
    if not isinstance(d, dict):
        return None
    otel = d.get(_OTEL_KEY)
    if not isinstance(otel, dict):
        return None
    val = otel.get("trace_id")
    if isinstance(val, str) and _TRACE_ID_HEX.match(val):
        return val.lower()
    return None


def _trace_id_from_json_text(text: str) -> str | None:
    """Try to decode `text` as JSON and pull trace_id from the resulting dict.

    Some MCP clients serialize `structuredContent` into a JSON string in a
    text block instead of passing it through as a `{json: {...}}` block —
    especially when bridging into Bedrock Converse's tool_result shape.
    """
    text = text.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return _trace_id_from_dict(parsed)


def _extract_trace_id(content: Any) -> str | None:
    """Pull the MCP `$opentelemetry.trace_id` from a tool_result payload.

    Handles both the Anthropic tool_result shape (string or list of blocks)
    and the Converse toolResult shape (list of `{text}` / `{json}` / etc.
    blocks). Returns None when the marker is absent.
    """
    if content is None:
        return None
    if isinstance(content, str):
        return _trace_id_from_json_text(content)
    if isinstance(content, dict):
        # Rare — some clients pass structured content directly here.
        return _trace_id_from_dict(content)
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        # Converse `{json: {...}}` carries structured content natively.
        if isinstance(block.get("json"), dict):
            trace_id = _trace_id_from_dict(block["json"])
            if trace_id:
                return trace_id
        # Text (Anthropic `{type: "text", text}`, Converse `{text}`) —
        # may be a JSON-encoded structuredContent envelope.
        text = block.get("text")
        if isinstance(text, str):
            trace_id = _trace_id_from_json_text(text)
            if trace_id:
                return trace_id
    return None


def _iter_content_blocks(messages: list[Any]) -> Any:
    """Yield each content block across all messages (skipping malformed entries)."""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict):
                yield block


def _used_tools(record: dict[str, Any], raw_name_to_id: dict[str, str]) -> list[AIToolUse]:
    """Collect completed tool invocations visible in this record.

    Only tool_result blocks in the input *after the last assistant message*
    are emitted — that's the pair-complete slice for this turn (the
    assistant just consumed those results). Their tool_id is resolved by
    matching `tool_use_id` against prior tool_use blocks in the same input
    history. Anthropic and Converse shapes handled side-by-side.

    In-flight calls (a `tool_use` sitting in this record's output with no
    result yet) are deferred — the pair will appear on the next invocation
    once the client posts the result back.
    """
    input_body = (record.get("input") or {}).get("inputBodyJson")
    if not isinstance(input_body, dict):
        return []
    input_messages: list[Any] = list(input_body.get("messages") or [])

    # tool_use_id → raw tool name, from prior assistant tool_use blocks
    # (both Anthropic and Converse shapes).
    name_by_use_id: dict[str, str] = {}
    for block in _iter_content_blocks(input_messages):
        if block.get("type") == "tool_use":
            uid = block.get("id")
            name = block.get("name")
            if isinstance(uid, str) and isinstance(name, str):
                name_by_use_id[uid] = name
        elif isinstance(block.get("toolUse"), dict):
            tu = block["toolUse"]
            uid = tu.get("toolUseId")
            name = tu.get("name")
            if isinstance(uid, str) and isinstance(name, str):
                name_by_use_id[uid] = name

    # Fresh region: everything after the last assistant message. Earlier
    # tool_results were already reported on prior invocation events.
    last_assistant = max(
        (
            i
            for i, m in enumerate(input_messages)
            if isinstance(m, dict) and m.get("role") == "assistant"
        ),
        default=-1,
    )
    fresh_messages = input_messages[last_assistant + 1 :]

    used: list[AIToolUse] = []
    seen: set[str] = set()
    for block in _iter_content_blocks(fresh_messages):
        if block.get("type") == "tool_result":
            uid = block.get("tool_use_id")
            # Anthropic: `is_error` is optional; absence = success.
            raw_is_error = block.get("is_error")
            is_error = raw_is_error if isinstance(raw_is_error, bool) else False
            trace_id = _extract_trace_id(block.get("content"))
        elif isinstance(block.get("toolResult"), dict):
            tr = block["toolResult"]
            uid = tr.get("toolUseId")
            # Converse: `status` is optional and only Claude 3 populates it.
            # Absence = success (there's no other outcome carrier on the wire).
            is_error = tr.get("status") == "error"
            trace_id = _extract_trace_id(tr.get("content"))
        else:
            continue
        if not isinstance(uid, str) or uid in seen:
            continue
        name = name_by_use_id.get(uid)
        tool_id = raw_name_to_id.get(name or "")
        if not tool_id:
            # Result references a tool we can't identify (tool_use missing
            # from input history, or toolConfig omits it). Skip — an entry
            # with no tool_id has no analytic value.
            continue
        seen.add(uid)
        used.append(AIToolUse(tool_id=tool_id, is_error=is_error, trace_id=trace_id))

    return used


async def build_event(
    record: dict[str, Any],
    *,
    include_raw_content: bool = False,
    model_region: str | None = None,
    max_content_size: int = 100_000,
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
    used = _used_tools(record, raw_to_id)

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
        used_tools=used or None,
        stop_reason=_stop_reason(record),
        input=_build_content(
            inp.get("inputBodyJson"),
            include_text=include_raw_content,
            max_content_size=max_content_size,
        ),
        output=_build_content(
            out.get("outputBodyJson"),
            include_text=include_raw_content,
            max_content_size=max_content_size,
        ),
        accessed_files=await _accessed_files(
            record,
            include_raw_content=include_raw_content,
            max_content_size=max_content_size,
        )
        or None,
    )
