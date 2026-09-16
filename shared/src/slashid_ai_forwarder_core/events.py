"""NormalizedInvocation + EventEnvelope → AIInvocationObservedV1 transformation.

Pure logic: no I/O, no vendor-specific record parsing. Vendor forwarders
(bedrock's ``event_envelope.bedrock_envelope``, future Vertex equivalent)
extract an ``EventEnvelope`` from their record shape and pass it in
alongside the canonical ``NormalizedInvocation``. Models mirror the
SlashID OpenAPI schemas (see ``~/slashid/ng-evangelion/spec/openapi.yaml``,
components ``AIInvocationObservedV1`` et al).
``model_dump(mode="json", exclude_none=True)`` produces wire-compatible
payloads.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .content_utils import truncate_middle
from .normalize.normalized.otel import extract_otel
from .normalize.turn import after_last_assistant

if TYPE_CHECKING:
    # events.py already has ``from __future__ import annotations`` so
    # annotations resolve lazily. A runtime import here would create a
    # circular import at module load: types.py imports AIInvocationTokens /
    # AIStopReason / AITool / AIToolServer from events.py.
    from .config_base import BaseConfig
    from .normalize.normalized.types import NormalizedInvocation

log = logging.getLogger(__name__)


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
    "error",
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

    `tool_use_id` is the inference-provider-generated id that ties the
    tool_use and tool_result blocks together (Bedrock `toolUse.toolUseId`,
    Anthropic `tool_use.id`). Treated as effectively globally unique for
    cross-log correlation. The same id naturally reappears in every
    subsequent turn's conversation history, but always pointing at the
    same logical invocation.

    `trace_id` / `span_id` are the OTel context echoed by MCP servers via a
    `$opentelemetry` block in `structuredContent` (see mcp-gate-demo
    CorrelationIdMiddleware) — they correlate this specific tool call with
    the corresponding Gate observability event on the SlashID side.
    Absent when the MCP server doesn't run the middleware.
    """

    tool_id: str
    is_error: bool
    tool_use_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


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

    ``principal_arn`` identifies the caller; ``access_key_id`` enables the
    server's AssumeRole-chain unrolling when set.

    ``kind`` is the discriminator field for the ``identity_details``
    union (``AWSIdentityDetails | GCPIdentityDetails``). Defaults to
    ``"aws"``. Downstream consumers that ``model_validate`` events off
    disk must include ``"kind": "aws"`` in serialized identity_details
    dicts.
    """

    kind: Literal["aws"] = "aws"
    principal_arn: str
    access_key_id: str | None = None
    # Placeholder — not populated by any forwarder yet. Bedrock's Model
    # Invocation Logging carries only ``arn`` / ``resolved_arn`` /
    # ``accessKeyId``; the MFA flag lives in CloudTrail
    # (``userIdentity.sessionContext.attributes.mfaAuthenticated``, a
    # *string* ``"true"``/``"false"`` on the wire there). Populating it
    # needs a MIL x CloudTrail join — the AWS analogue of the Vertex
    # BQ x Cloud-Audit-Log correlation. Tri-state on purpose: ``None``
    # means "not observed", not "no MFA".
    mfa_authenticated: bool | None = None


class GCPCredential(_WireModel):
    """One credential in ``GCPIdentityDetails.credential_chain``.

    The chain runs root ([0]) → effective principal ([-1]), with any
    impersonation hops in between. ``oauth_client_id`` is populated on
    the effective credential ([-1]): the gcloud/ADC client ID for a
    direct call, the service account's numeric ``uniqueId`` for an
    impersonated one. Root and intermediate hops are oauth-less — the
    audit log doesn't preserve the root's OAuth flow across a hop.
    """

    principal_email: str | None = None
    principal_subject: str | None = None
    oauth_client_id: str | None = None


class GCPIdentityDetails(_WireModel):
    """GCP-source shape of ``AIInvocationObservedV1.identity_details``.

    ``credential_chain`` captures the full auth path from the original
    credential ([0]) to the effective principal ([-1], the one the API
    sees). For non-impersonated calls, chain has length 1 where root
    == effective. For ``--impersonate-service-account`` and similar
    flows, chain has length ≥ 2.

    ``credential_chain = None`` means identity resolution failed — the
    Vertex identity-correlation phase saw either no matching audit
    entries or entries that disagreed at both endpoints of the chain.
    """

    kind: Literal["gcp"] = "gcp"
    credential_chain: list[GCPCredential] | None = None


IdentityDetails = Annotated[
    AWSIdentityDetails | GCPIdentityDetails,
    Field(discriminator="kind"),
]


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
    identity_details: IdentityDetails
    model: AIModel
    tokens: AIInvocationTokens = Field(default_factory=AIInvocationTokens)
    # Name of the vendor format the record's outputBodyJson matched — set
    # by the envelope normalizer from its format-table entry (e.g.
    # "anthropic-message", "anthropic-stream", "bedrock-converse"). Value
    # "unknown" means no format matched: semantic fields (stop_reason,
    # used_tools, ...) are empty or best-effort. Convention: kebab-case
    # ``<vendor>-<shape>`` per envelope.
    parsed_as: str
    available_agents: list[AIAgentDetails] | None = None
    used_agent_ids: list[str] | None = None
    available_tool_servers: list[AIToolServer] | None = None
    available_tools: list[AITool] | None = None
    used_tools: list[AIToolUse] | None = None
    stop_reason: AIStopReason | None = None
    # Client that issued the call (Vertex: audit-log
    # ``requestMetadata.callerSuppliedUserAgent``). ``None`` when the
    # source doesn't carry it — Bedrock's MIL records have no
    # user-agent field, so it stays null there until a CloudTrail join
    # lands.
    user_agent: str | None = None
    conversation_id: str | None = None
    input: AIInvocationContent | None = None
    output: AIInvocationContent | None = None
    accessed_files: list[AIAccessedFile] | None = None


class EventEnvelope(_WireModel):
    """Vendor-neutral inputs to ``build_event_from_normalized``.

    Every field maps 1:1 onto a top-level ``AIInvocationObservedV1``
    field the record-derived envelope owns (as opposed to
    normalized-conversation-derived fields like ``available_tools``,
    ``used_tools``, ``stop_reason``, ``input``/``output``,
    ``accessed_files``, which stay with the pure shared builder).
    Vendor-specific envelope constructors
    (Bedrock's ``bedrock_envelope``, Vertex's future equivalent) return
    ``None`` on drop conditions and populate an ``EventEnvelope``
    otherwise; the shared builder never has to touch a raw record.

    ``identity_details`` is the same discriminated union as on
    ``AIInvocationObservedV1``: vendor-specific envelope constructors
    populate an ``AWSIdentityDetails`` or ``GCPIdentityDetails`` and the
    shared builder passes it through unchanged.
    """

    request_id: str
    timestamp: str
    identity_details: IdentityDetails
    model: AIModel
    tokens: AIInvocationTokens = Field(default_factory=AIInvocationTokens)
    parsed_as: str
    # Client that issued the call, as the vendor recorded it. Top-level
    # rather than under ``identity_details`` because it describes the
    # request, not the principal — and it's cross-cloud, whereas the
    # identity shapes are vendor-specific.
    user_agent: str | None = None
    # True when the vendor recorded a server-side error for the request
    # (non-zero gRPC status on Cloud Audit Logs, non-2xx HTTP status on
    # BQ payload rows, ``error`` set on Bedrock/Converse). When True the
    # shared builder overrides ``stop_reason`` to ``"error"`` — an
    # errored request has no legit ``end_turn`` / ``max_tokens`` /
    # ``tool_use`` etc.
    is_error: bool = False


# --- record parsing ---------------------------------------------------------


# Fields that carry customer prompt/response bodies when
# ``SLASHID_INCLUDE_RAW_CONTENT`` is set. Wire delivery to the SlashID
# sink is fine — that's the customer's own destination — but ops-side
# CloudWatch / Cloud Function logs shouldn't leak them.
_LOG_REDACTED_FIELDS: frozenset[str] = frozenset({"redacted_text", "redacted_content"})


def redact_for_logging(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with sensitive fields recursively
    stripped, suitable for ops-side logging (CloudWatch, Cloud Function
    stdout, structured log aggregators).

    Removes ``redacted_text`` on ``AIInvocationContent`` and
    ``redacted_content`` on ``AIAccessedFile`` at any depth. The hash /
    mime / byte_length siblings stay in place so the log line still
    lets ops correlate an event without exposing the underlying bytes.

    Handlers call this before ``json.dumps``:

        log.info("event: %s", json.dumps(
            redact_for_logging(event.model_dump(mode="json", exclude_none=True)),
            separators=(",", ":"),
        ))
    """
    return _strip_sensitive(payload)


def _strip_sensitive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_sensitive(v) for k, v in obj.items() if k not in _LOG_REDACTED_FIELDS}
    if isinstance(obj, list):
        return [_strip_sensitive(i) for i in obj]
    return obj


def _strip_empty_top(d: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is an empty container.

    Preserves the pre-drive-by wire content-hash stability: before the
    non-null-default-list refactor, empty list fields on
    NormalizedInvocationInput were serialized as ``None`` and dropped by
    ``exclude_none=True``. Now they're always ``[]`` and would otherwise
    show up in the dumped dict as ``{"tools_declared": [], "tool_servers": []}``
    — changing every content hash. Strip at the boundary so the hashed
    bytes match pre-drive-by behavior.
    """
    return {k: v for k, v in d.items() if v}


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


def _used_tools(normalized: NormalizedInvocation) -> list[AIToolUse]:
    """Collect completed tool invocations from the canonical input messages.

    Only tool_result blocks after the last assistant message count — that's
    the pair-complete slice for this turn (the assistant just consumed
    those results). tool_id correlation goes:

        tool_result.tool_use_id
          → NormalizedContent(kind="tool_use", tool_name=<raw wire name>)
          → parse_tool_name → (parsed_name, server_name)
          → (server_name, parsed_name) → AITool.id (via tools_declared)

    In-flight calls (``tool_use`` in this record's output with no matching
    ``tool_result`` yet) are deferred to the next invocation event.
    """
    input_messages = normalized.input.messages
    if not input_messages:
        return []

    # (server_name, parsed_tool_name) → AITool.id
    tools = normalized.input.tools_declared
    servers = normalized.input.tool_servers
    server_name_by_id = {s.id: s.name or "builtin" for s in servers}
    id_by_key: dict[tuple[str, str], str] = {}
    for tool in tools:
        server_name = server_name_by_id.get(tool.tool_server_id or "") or "builtin"
        if tool.name:
            id_by_key[(server_name, tool.name)] = tool.id

    # tool_use_id → raw wire tool name, from any tool_use block in the history.
    name_by_use_id: dict[str, str] = {}
    for msg in input_messages:
        for block in msg.content:
            if block.kind == "tool_use" and block.tool_use_id and block.tool_name:
                name_by_use_id[block.tool_use_id] = block.tool_name

    used: list[AIToolUse] = []
    seen: set[str] = set()
    for msg in after_last_assistant(input_messages):
        for block in msg.content:
            if block.kind != "tool_result" or not block.tool_use_id:
                continue
            uid = block.tool_use_id
            if uid in seen:
                continue
            raw_name = name_by_use_id.get(uid)
            if not raw_name:
                continue
            parsed_name, server_name, _kind = parse_tool_name(raw_name)
            tool_id = id_by_key.get((server_name, parsed_name))
            if not tool_id:
                # Result references a tool we can't identify (tool_use
                # missing from input history, or tools_declared omits it).
                # Skip — an entry with no tool_id has no analytic value.
                continue
            trace_id, span_id = extract_otel(block.tool_output)
            seen.add(uid)
            used.append(
                AIToolUse(
                    tool_id=tool_id,
                    tool_use_id=uid,
                    is_error=block.tool_is_error,
                    trace_id=trace_id,
                    span_id=span_id,
                )
            )
    return used


async def build_event_from_normalized(
    normalized: NormalizedInvocation,
    envelope: EventEnvelope,
    *,
    config: BaseConfig,
) -> AIInvocationObservedV1:
    """Assemble an AIInvocationObservedV1 from a canonical invocation + envelope.

    Pure: reads only from ``normalized`` (canonical conversation shape)
    and ``envelope`` (vendor-neutral top-level metadata). No record
    access. Never returns ``None`` — record-level drop decisions
    (missing requestId, missing identity) live in the vendor envelope
    constructor; if the caller produced an ``EventEnvelope`` at all,
    this always produces an event.

    ``config.include_raw_content`` defaults to off — by default we send
    hash, mime, and byte length on ``input``/``output`` but no prompt
    text. Flip via the SLASHID_INCLUDE_RAW_CONTENT env var (CFN
    parameter same name). ``_build_content`` stays typed on primitives;
    we unpack the config here at the boundary.
    """
    servers = normalized.input.tool_servers
    tools = normalized.input.tools_declared
    used = _used_tools(normalized)

    return AIInvocationObservedV1(
        request_id=envelope.request_id,
        timestamp=envelope.timestamp,
        identity_details=envelope.identity_details,
        model=envelope.model,
        tokens=envelope.tokens,
        parsed_as=envelope.parsed_as,
        user_agent=envelope.user_agent,
        available_tool_servers=servers or None,
        available_tools=tools or None,
        used_tools=used or None,
        stop_reason="error" if envelope.is_error else normalized.output.stop_reason,
        input=_build_content(
            _strip_empty_top(normalized.input.model_dump(mode="json", exclude_none=True)),
            include_text=config.include_raw_content,
            max_content_size=config.max_content_size,
        ),
        output=_build_content(
            _strip_empty_top(normalized.output.model_dump(mode="json", exclude_none=True)),
            include_text=config.include_raw_content,
            max_content_size=config.max_content_size,
        ),
        accessed_files=normalized.accessed_files or None,
    )


def build_sparse_event(
    envelope: EventEnvelope,
    *,
    config: BaseConfig,
) -> AIInvocationObservedV1:
    """Assemble an ``AIInvocationObservedV1`` from an envelope alone.

    For sources that observe an invocation happened but can see nothing
    about its shape — Cloud Audit Logs for non-Google Vertex publishers,
    Purview UAL entries, similar audit-envelope-only paths. All
    conversation-shaped fields (``available_tools``, ``used_tools``,
    ``input``, ``output``, ``accessed_files``) stay ``None`` at the type
    level rather than being nulled after the fact.

    ``stop_reason`` is ``"error"`` when the envelope flags an error and
    ``None`` otherwise — consistent with the ``is_error`` override in
    ``build_event_from_normalized``.

    ``config`` is unused today but kept in the signature so the sparse
    builder can pick up any config-gated cross-cutting later (payload
    redaction, per-tenant policies) without a caller-visible change.
    """
    del config
    return AIInvocationObservedV1(
        request_id=envelope.request_id,
        timestamp=envelope.timestamp,
        identity_details=envelope.identity_details,
        model=envelope.model,
        tokens=envelope.tokens,
        parsed_as=envelope.parsed_as,
        user_agent=envelope.user_agent,
        stop_reason="error" if envelope.is_error else None,
    )
