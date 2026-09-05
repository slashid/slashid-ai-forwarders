"""Bedrock MIL record → shared ``EventEnvelope``.

The vendor-specific half of the event build. Everything that reads
directly from a MIL record's shape lives here: identity extraction,
timestamp normalization, stop-reason coercion, top-level token
counts, and the model-catalog lookup. Produces a vendor-neutral
``EventEnvelope`` (or ``None`` on drop) which the shared pure
``build_event_from_normalized`` turns into an
``AIInvocationObservedV1``.

Pure logic, no I/O — model-catalog lookup is imported lazily to
avoid a top-level dependency on the catalog module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from slashid_ai_forwarder_core.events import (
    AIInvocationTokens,
    AIModel,
    AIStopReason,
    AWSIdentityDetails,
    EventEnvelope,
)

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


def bedrock_envelope(
    record: dict[str, Any],
    *,
    model_region: str | None = None,
) -> EventEnvelope | None:
    """Extract a vendor-neutral ``EventEnvelope`` from a Bedrock MIL record.

    Returns ``None`` when the record lacks a ``requestId`` (body-offload
    S3 objects share the listing prefix in some MIL layouts; they appear
    as pseudo-records with no identifying metadata) or has no usable
    principal ARN (server would reject ``identity_details`` anyway, and
    a placeholder would pollute the AI subgraph).

    ``model_region`` overrides ``record["region"]`` for the catalog
    lookup — reserved for the CFN-parameter path in bedrock/config where
    the invoked region isn't the record's region (cross-region inference
    profiles). Handler doesn't thread it yet; kept in the signature so
    the seam is ready when it does.
    """
    if not record.get("requestId"):
        return None

    identity = _identity_details(record)
    if identity is None:
        return None

    inp = record.get("input") or {}
    out = record.get("output") or {}
    raw_model_id = str(record.get("modelId") or "")

    region = model_region or str(record.get("region") or "")
    model_info = None
    if raw_model_id and region:
        # Lazy import matches the pre-split behavior: catalog module is
        # only pulled in when a real record is being processed, keeping
        # cold-start import graph minimal.
        from slashid_ai_forwarder_core.model_catalog import get_model_info

        model_info = get_model_info(raw_model_id, region)

    # model.id: use raw when it's already an ARN, else catalog ARN, else raw
    model_id = (
        raw_model_id
        if raw_model_id.startswith("arn:")
        else (model_info["arn"] if model_info else raw_model_id)
    )

    return EventEnvelope(
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
        # ``_parsed_as`` is set by the envelope normalizer (bedrock's
        # mil_normalize.normalize_record). Defensive fallback to "unknown"
        # for code paths that skip normalization (none in production today).
        parsed_as=record.get("_parsed_as", "unknown"),
        stop_reason=_stop_reason(record),
    )
