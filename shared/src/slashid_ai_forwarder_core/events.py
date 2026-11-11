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
import re
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SkipValidation,
    field_validator,
    model_validator,
)

from .content_utils import truncate_middle
from .normalize.normalized.otel import extract_otel
from .normalize.turn import after_last_assistant
from .rounds import round_links

if TYPE_CHECKING:
    # events.py already has ``from __future__ import annotations`` so
    # annotations resolve lazily. A runtime import here would create a
    # circular import at module load: types.py imports AIInvocationTokens /
    # AIStopReason / AITool / AIToolServer from events.py.
    from .config_base import BaseConfig
    from .normalize.normalized.types import NormalizedInvocation, NormalizedMessage

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
    """A tool invocation within an AI turn. In `used_tools`, a call whose
    result the model consumed; `is_error` is always set there. In
    `requested_tool_uses`, a call the model asked for that has not run;
    `is_error` is absent.

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

    `arguments` is a requested call's input as the model wrote it, set only
    on events sent to the AI preflight (see ``preflight_arguments``), so a
    policy can judge what the call would do. The server evaluates it and
    never stores it; ``redact_for_logging`` drops it. It skips validation
    because ``str_strip_whitespace`` would otherwise rewrite the strings
    inside it, and a policy must test the values the call carries.
    """

    tool_id: str
    is_error: bool | None = None
    tool_use_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    arguments: Annotated[dict[str, Any] | None, SkipValidation] = None


# The largest call input sent to the AI preflight, as compact JSON. Bigger
# input (a Write of a whole file) is left out rather than risk the request:
# a rule on arguments then reports that they were not sent, and every other
# rule still judges the call.
MAX_PREFLIGHT_ARGUMENTS_BYTES = 64 * 1024


def preflight_arguments(tool_input: object) -> dict[str, Any] | None:
    """A call's input as `AIToolUse.arguments`: an object, as the model
    wrote it, no larger than ``MAX_PREFLIGHT_ARGUMENTS_BYTES``; else None."""
    if not isinstance(tool_input, dict):
        return None
    arguments: dict[str, Any] = {str(k): v for k, v in tool_input.items()}
    size = len(json.dumps(arguments, separators=(",", ":"), ensure_ascii=False).encode())
    if size > MAX_PREFLIGHT_ARGUMENTS_BYTES:
        return None
    return arguments


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

    ``principal_arn`` is the role or user the calls run as; ``gateway_arn`` the
    AgentCore Gateway that served the call. Either may be unknown, and an identity
    can be empty. ``access_key_id`` enables the server's AssumeRole-chain
    unrolling when set.

    ``kind`` is the discriminator field for the ``identity_details``
    union (``AWSIdentityDetails | GCPIdentityDetails``). Defaults to
    ``"aws"``. Downstream consumers that ``model_validate`` events off
    disk must include ``"kind": "aws"`` in serialized identity_details
    dicts.
    """

    kind: Literal["aws"] = "aws"
    principal_arn: str | None = None
    gateway_arn: str | None = None
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


class AnthropicIdentityDetails(_WireModel):
    """Anthropic-source shape of ``AIInvocationObservedV1.identity_details``.

    The Inference hooks frame names the acting principal as ``actor.id``,
    a ``user_01…`` identifier stable across requests; the compliance
    denial activity names the same principal as ``actor.user_id``.

    Required, not optional: the server's resolver rejects a payload with
    no identifier, so a frame whose actor id is null is dropped by the
    envelope constructor rather than turned into an unusable event.
    ``kind`` is a client-side discriminator; the server ignores it.
    """

    kind: Literal["anthropic"] = "anthropic"
    # A service account's ``svac_…`` id. An identity in its own right and
    # the highest-priority branch server-side: when a service account
    # acted, it IS the principal rather than a stand-in for a human.
    service_account_id: str | None = None
    # The acting principal's stable ``user_01…`` handle. The hooks frame
    # names it ``actor.id``; the denial activity names the same principal
    # ``actor.user_id``. This is the only one this forwarder can observe.
    user_id: str | None = None
    # An API key's ``apikey_…`` id. A credential, not an identity, so it
    # resolves indirectly — to whoever the key metadata records as creator.
    api_key_id: str | None = None
    # Lowercase sha256 of the RAW key, all an inline proxy can see, since
    # raw keys never map back to an ``apikey_…``. Meters without naming a
    # principal, so an event carrying only this resolves to no identity.
    api_key_hash: str | None = None

    @model_validator(mode="after")
    def _at_least_one_identifier(self) -> AnthropicIdentityDetails:
        """No single producer observes all of these, so each is optional —
        but a payload naming nobody is rejected. There is nothing to
        resolve and nothing to meter, and the server rejects it too, so
        failing here turns a silent server-side drop into a local error."""
        if not (self.service_account_id or self.user_id or self.api_key_id or self.api_key_hash):
            raise ValueError("anthropic identity carries no identifier")
        return self


class OpenAIIdentityDetails(_WireModel):
    """OpenAI-source shape of ``AIInvocationObservedV1.identity_details``.

    Mirrors the server's ``OpenAIIdentityDetails`` in
    ``entity_sources/openai/ai_invocation.go``. Codex sets ``user_id``, the
    ChatGPT workspace ``user-…`` id.
    """

    kind: Literal["openai"] = "openai"
    service_account_id: str | None = None
    user_id: str | None = None
    api_key_id: str | None = None
    api_key_hash: str | None = None

    @model_validator(mode="after")
    def _at_least_one_identifier(self) -> OpenAIIdentityDetails:
        if not (self.service_account_id or self.user_id or self.api_key_id or self.api_key_hash):
            raise ValueError("openai identity carries no identifier")
        return self


class AIGatewayIdentityDetails(_WireModel):
    """AI-gateway-source shape of ``AIInvocationObservedV1.identity_details``.

    A gateway (Portkey) sits in front of every provider, so the provider's
    own principal is the gateway's key, the same for every caller. Who
    acted is what the calling application says in the request's metadata:
    the person (``user_email``, or ``user_id`` when it is not an address)
    and, when the call runs on an agent's behalf, the agent (``agent_id``).
    """

    kind: Literal["ai_gateway"] = "ai_gateway"
    user_email: str | None = None
    user_id: str | None = None
    agent_id: str | None = None

    @model_validator(mode="after")
    def _at_least_one_identifier(self) -> AIGatewayIdentityDetails:
        if not (self.user_email or self.user_id or self.agent_id):
            raise ValueError("ai_gateway identity carries no identifier")
        return self


IdentityDetails = Annotated[
    AWSIdentityDetails
    | GCPIdentityDetails
    | AnthropicIdentityDetails
    | OpenAIIdentityDetails
    | AIGatewayIdentityDetails,
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

    # A file name, never a path: whatever a producer passes, the directories
    # stay on the machine (or in the provider's bucket).
    name: str | None = None
    content_hashes: dict[str, str] | None = None
    media_type: str | None = None
    byte_length: int | None = None
    redacted_content: str | None = None
    # How the file entered the conversation. A compliance reader rebuilds
    # ``attachment`` entries from the provider's own listing and must never
    # touch ``tool_result`` ones, which a flat list cannot express.
    # ``generated`` is reserved for files a tool wrote and is not emitted:
    # nothing produces it yet, and advertising an unset value is worse than
    # adding the member later.
    provenance: Literal["tool_result", "attachment"] | None = None
    # A connector file's provider identifiers (``google/<fileId>``,
    # ``m365/item/<driveId>/<itemId>``, ``m365/guid/<GUID>``,
    # ``m365/path/<driveId>/<path>``), every one the call gave. A content hash cannot
    # stand for them: a connector returns a re-rendering, not the stored bytes.
    file_refs: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _file_name_only(cls, v: str | None) -> str | None:
        # The Windows flavour reads both separators on any host, and drive
        # letters, UNC shares and ``scheme://`` prefixes fall away with the rest.
        return (PureWindowsPath(v).name or None) if v is not None else None


class AIInvocationObservedV1(_WireModel):
    """spec/openapi.yaml — AIInvocationObservedV1. Body for POST /ip/nhi/events/ai-invocations.

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
    # Client tool calls in this event's output, not yet run.
    requested_tool_uses: list[AIToolUse] | None = None
    stop_reason: AIStopReason | None = None
    # Client that issued the call (Vertex: audit-log
    # ``requestMetadata.callerSuppliedUserAgent``). ``None`` when the
    # source doesn't carry it — Bedrock's MIL records have no
    # user-agent field, so it stays null there until a CloudTrail join
    # lands.
    user_agent: str | None = None
    conversation_id: str | None = None
    # Stitching hashes (see ``rounds.py``). ``round_hash`` is absent when the
    # event has no response. ``recent_round_hashes`` is newest first and ends
    # with ``"start"`` when it reaches the first round and ``"..."`` when not.
    round_hash: str | None = None
    recent_round_hashes: list[str] | None = None
    input: AIInvocationContent | None = None
    output: AIInvocationContent | None = None
    accessed_files: list[AIAccessedFile] | None = None


class AIPreflightResponse(BaseModel):
    """spec/ai-schemas.yaml — AIPreflightResponse. Empty ``deny_reasons``
    allows, non-empty denies. A response, so unknown fields are ignored
    rather than forbidden: a field the server adds must not fail the check."""

    deny_reasons: list[str]


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
    # The vendor's own identifier for the multi-turn conversation this
    # invocation belongs to. Set by sources that have one; Bedrock and
    # Vertex leave it unset. It is what groups a denial incident, since
    # one sticky denial produces an event per subsequent turn.
    conversation_id: str | None = None
    # True when the vendor recorded a server-side error for the request
    # (non-zero gRPC status on Cloud Audit Logs, non-2xx HTTP status on
    # BQ payload rows, ``error`` set on Bedrock/Converse). When True the
    # shared builder overrides ``stop_reason`` to ``"error"`` — an
    # errored request has no legit ``end_turn`` / ``max_tokens`` /
    # ``tool_use`` etc.
    is_error: bool = False
    # The history does not reach the conversation's first round (e.g. Codex
    # fork whose parent is gone); round links end in "...".
    history_truncated: bool = False


# --- record parsing ---------------------------------------------------------


# Fields that carry customer prompt/response bodies when
# ``SLASHID_INCLUDE_RAW_CONTENT`` is set. Wire delivery to the SlashID
# sink is fine — that's the customer's own destination — but ops-side
# CloudWatch / Cloud Run logs shouldn't leak them.
_LOG_REDACTED_FIELDS: frozenset[str] = frozenset({"redacted_text", "redacted_content", "arguments"})


def redact_for_logging(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with sensitive fields recursively
    stripped, suitable for ops-side logging (CloudWatch, Cloud Run
    stdout, structured log aggregators).

    Removes ``redacted_text`` on ``AIInvocationContent``,
    ``redacted_content`` on ``AIAccessedFile`` and ``arguments`` on
    ``AIToolUse`` at any depth. The hash /
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


_MCP_UUID_TOOL = re.compile(
    r"^mcp_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_(.+)$"
)


def parse_tool_name(name: str) -> tuple[str, str, AIToolServerKind]:
    """Split a wire tool name into (tool_name, server_name, server_kind).

    - `mcp__{server}__{tool}`  → (`{tool}`, `{server}`, `"mcp"`)
    - `mcp_{uuid}_{tool}`      → (`{tool}`, `{uuid}`, `"mcp"`) — claude.ai names a
      connector's tools by the connector's UUID
    - `{server}:{tool}`        → (`{tool}`, `{server}`, `"mcp"`) — claude.ai's
      `Microsoft 365:outlook_email_search` (the first `:` splits; both sides non-empty)
    - `{server}#{tool}`        → (`{tool}`, `{server}`, `"mcp"`) — claude.ai's
      `workspace#bash`
    - `{server}__{tool}`       → (`{tool}`, `{server}`, `"runtime"`)
    - `{tool}`                 → (`{tool}`, `"builtin"`, `"runtime"`)
      (bare tools share one synthetic `builtin` server; `runtime` is the
       AIToolServerDetails.kind enum value the SlashID schema uses for
       any non-MCP server.)

    The server name comes back as spelled on the wire; ids are derived from
    ``canonical_server_name`` of it, so spellings of one server share them.
    """
    if name.startswith("mcp__"):
        rest = name[len("mcp__") :]
        if "__" in rest:
            server, tool = rest.split("__", 1)
            return tool, server, "mcp"
        return rest, "builtin", "runtime"
    if m := _MCP_UUID_TOOL.match(name):
        return m.group(2), m.group(1), "mcp"
    for sep in (":", "#"):
        server, found, tool = name.partition(sep)
        if found and server and tool:
            return tool, server, "mcp"
    if "__" in name:
        server, tool = name.split("__", 1)
        return tool, server, "runtime"
    return name, "builtin", "runtime"


def canonical_server_name(name: str) -> str:
    """The spelling-independent key of a tool server's name.

    One connector reaches us as `Microsoft 365`, `microsoft-365`, `microsoft_365`
    and `Microsoft_365`, depending on the surface. ASCII letters are lowercased;
    every run of anything else that is not an ASCII letter or digit becomes one
    `_`; `_` is stripped at both ends. A name with nothing left keeps itself.

    Must stay identical to ``canonicalMCPServerName`` in ng-evangelion
    (backend/modules/nhi/adapters/entity_sources/anthropic/managed_agents.go):
    both sides derive the same server id from it.
    """
    lowered = "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in name)
    canonical = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    return canonical or name


def _tool_id_index(normalized: NormalizedInvocation) -> dict[tuple[str, str], str]:
    """(canonical server name, parsed_tool_name) → AITool.id over the declared tools."""
    server_name_by_id = {s.id: s.name or "builtin" for s in normalized.input.tool_servers}
    id_by_key: dict[tuple[str, str], str] = {}
    for tool in normalized.input.tools_declared:
        server_name = server_name_by_id.get(tool.tool_server_id or "") or "builtin"
        if tool.name:
            id_by_key[(canonical_server_name(server_name), tool.name)] = tool.id
    return id_by_key


def _requested_tool_uses(normalized: NormalizedInvocation) -> list[AIToolUse]:
    """Tool calls the model asked for in this invocation's output, not yet run."""
    message = normalized.output.message
    if message is None:
        return []
    ids = _tool_id_index(normalized)
    # A call whose result the answer carries inline already ran: it is in used_tools.
    ran = {b.tool_use_id for b in normalized.output.tool_results if b.tool_use_id}
    requested: list[AIToolUse] = []
    for block in message.content:
        if block.kind != "tool_use" or not block.tool_name or block.tool_executor == "server":
            continue
        if block.tool_use_id in ran:
            continue
        name, server, _kind = parse_tool_name(block.tool_name)
        tool_id = ids.get((canonical_server_name(server), name))
        if tool_id:
            requested.append(AIToolUse(tool_id=tool_id, tool_use_id=block.tool_use_id))
    return requested


def _tool_names_by_use_id(messages: list[NormalizedMessage]) -> dict[str, str]:
    names: dict[str, str] = {}
    for msg in messages:
        for block in msg.content:
            if block.kind == "tool_use" and block.tool_use_id and block.tool_name:
                names[block.tool_use_id] = block.tool_name
    return names


def used_tools_of(normalized: NormalizedInvocation) -> list[AIToolUse]:
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

    A chat answer carries its results inline (``output.tool_results``),
    run inside the response itself: those count here too, as this
    invocation's, paired with the ``tool_use`` blocks of the answer.
    """
    input_messages = normalized.input.messages
    inline_results = normalized.output.tool_results
    if not input_messages and not inline_results:
        return []

    id_by_key = _tool_id_index(normalized)

    # tool_use_id → raw wire tool name, from any tool_use block in the history.
    # An inline result pairs with the answer's own tool_use instead: a vendor
    # that reuses call ids across rounds (OpenAI chat's `call_0`) must not let
    # the answer's pending call rename a result the history already paired.
    name_by_use_id = _tool_names_by_use_id(input_messages)
    answer = normalized.output.message
    inline_name_by_use_id = _tool_names_by_use_id([answer] if answer else [])

    results = [
        (b, name_by_use_id) for msg in after_last_assistant(input_messages) for b in msg.content
    ]
    used: list[AIToolUse] = []
    seen: set[str] = set()
    for block, names in [*results, *((b, inline_name_by_use_id) for b in inline_results)]:
        if block.kind != "tool_result" or not block.tool_use_id:
            continue
        uid = block.tool_use_id
        if uid in seen:
            continue
        raw_name = names.get(uid)
        if not raw_name:
            continue
        parsed_name, server_name, _kind = parse_tool_name(raw_name)
        tool_id = id_by_key.get((canonical_server_name(server_name), parsed_name))
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

    ``input`` hashes the messages alone, limited to the round the model
    consumed unless ``config.input_scope`` is ``session``. Tool metadata has
    its own top-level fields.
    """
    servers = normalized.input.tool_servers
    tools = normalized.input.tools_declared
    used = used_tools_of(normalized)
    requested = _requested_tool_uses(normalized)
    messages = normalized.input.messages
    hashed = after_last_assistant(messages) if config.input_scope == "round" else messages
    answer = normalized.output.message
    round_hash, recent_round_hashes = round_links(
        [*messages, answer] if answer is not None else messages,
        depth=config.round_link_depth,
        truncated=envelope.history_truncated,
    )

    return AIInvocationObservedV1(
        request_id=envelope.request_id,
        timestamp=envelope.timestamp,
        identity_details=envelope.identity_details,
        model=envelope.model,
        tokens=envelope.tokens,
        parsed_as=envelope.parsed_as,
        user_agent=envelope.user_agent,
        conversation_id=envelope.conversation_id,
        available_tool_servers=servers or None,
        available_tools=tools or None,
        used_tools=used or None,
        requested_tool_uses=requested or None,
        stop_reason="error" if envelope.is_error else normalized.output.stop_reason,
        round_hash=round_hash,
        recent_round_hashes=recent_round_hashes or None,
        input=_build_content(
            [m.model_dump(mode="json", exclude_none=True) for m in hashed],
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
        conversation_id=envelope.conversation_id,
        stop_reason="error" if envelope.is_error else None,
    )
