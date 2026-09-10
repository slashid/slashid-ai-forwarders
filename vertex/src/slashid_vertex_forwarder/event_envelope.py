"""Vertex records → shared ``EventEnvelope``.

Mirrors ``bedrock/event_envelope.py`` — the vendor-specific half of the
event build. Two builders share this file because both paths (BQ payload
via ``vertex_envelope``; audit-log-only via ``vertex_audit_only_envelope``)
map onto the same wire schema and use the shared ``_parse_model_path``
helper.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from slashid_ai_forwarder_core.events import (
    AIInvocationTokens,
    AIModel,
    EventEnvelope,
    GCPIdentityDetails,
)

from .event_source import Entry

if TYPE_CHECKING:
    from .audit_source import AuditEntry

# Wire ``parsed_as`` values — the record shape's identifier. Downstream
# consumers pattern-match on these to know which fields are populated:
# ``vertex-google`` events (BQ payload path) carry full input/output/
# tokens/stop_reason; ``vertex-audit`` events (non-Google publishers)
# are sparse — identity + model reference only. Provider + model.name
# on ``AIModel`` differentiate publishers within each shape.
PARSED_AS_GOOGLE = "vertex-google"
PARSED_AS_AUDIT = "vertex-audit"


_MODEL_PATH_RE = re.compile(r"(?:.+/)?publishers/([^/]+)/models/(.+)$")


def _parse_model_path(path: str) -> tuple[str | None, str | None]:
    """Extract (publisher, model_name) from a Vertex path.

    Accepts either the short form ``publishers/<pub>/models/<model>``
    or the long form
    ``projects/<proj>/locations/<region>/publishers/<pub>/models/<model>``
    — the leading segment group is optional in the regex. Greedy ``.+``
    on the model group preserves any embedded slashes (Vertex fine-tuned
    / deployed variants occasionally carry ``endpoints/<id>``-style
    suffixes).

    Returns (None, None) on any shape that doesn't match — callers drop
    the field rather than surface a partial parse.
    """
    match = _MODEL_PATH_RE.match(path)
    if match is None:
        return None, None
    return match.group(1) or None, match.group(2) or None


def _short_method(method_name: str) -> str:
    """``google.cloud.aiplatform.v1.PredictionService.RawPredict``
    → ``rawPredict``. Lowercase-first the tail segment after the final
    dot. Empty input → empty string."""
    tail = method_name.rsplit(".", 1)[-1]
    if not tail:
        return ""
    return tail[:1].lower() + tail[1:]


def vertex_audit_only_envelope(audit: AuditEntry) -> EventEnvelope | None:
    """Build the vendor-neutral ``EventEnvelope`` for one non-Google
    audit-log entry.

    Returns ``None`` when the entry's ``resource_name`` doesn't parse
    into a ``publishers/<pub>/models/<model>`` shape or when the
    ``insert_id`` is empty (should not happen given the Cloud Logging
    contract, but mirrors ``vertex_envelope``'s None-return shape).
    Sparse-by-design: tokens default to zero (audit logs carry no token
    counts), and ``stop_reason`` / ``input`` / ``output`` /
    ``used_tools`` / ``accessed_files`` are nulled by
    ``AuditOnlyEventSource`` post-build.
    """
    # Local import: audit_source imports GCPCredential from
    # slashid_ai_forwarder_core, not from this module — no cycle risk.
    # Keeping it lazy so tests can construct EventEnvelope directly
    # without dragging in google.cloud.logging shape validators.
    from .audit_source import _credential_chain

    if not audit.insert_id:
        return None
    publisher, model = _parse_model_path(audit.resource_name)
    if publisher is None or model is None:
        return None
    model_path = f"publishers/{publisher}/models/{model}"
    return EventEnvelope(
        request_id=audit.insert_id,
        timestamp=audit.timestamp.isoformat(),
        identity_details=GCPIdentityDetails(
            credential_chain=_credential_chain(audit) or None,
        ),
        model=AIModel(
            id=model_path,
            name=model,
            provider=publisher,
            raw_model_id=model_path,
        ),
        parsed_as=PARSED_AS_AUDIT,
    )


def vertex_envelope(entry: Entry) -> EventEnvelope | None:
    """Build the vendor-neutral ``EventEnvelope`` for one BQ entry.

    Returns ``None`` on drop conditions (missing request_id). BQ's
    request-response logging populates every row with a valid
    ``request_id``, so drops here are effectively "should not happen"
    — but the None return matches ``bedrock_envelope``'s shape.
    """
    if not entry.request_id:
        return None

    usage = entry.response_body.usageMetadata
    tokens = AIInvocationTokens(
        input=int(usage.promptTokenCount or 0),
        output=int(usage.candidatesTokenCount or 0),
        cache_read=int(usage.cachedContentTokenCount or 0),
        cache_write=0,  # Gemini does not expose a cache-write count
        reasoning=int(usage.thoughtsTokenCount or 0),
    )
    publisher, model_name = _parse_model_path(entry.model_path)

    return EventEnvelope(
        request_id=entry.request_id,
        timestamp=entry.logging_time.isoformat(),
        identity_details=entry.identity_details,
        model=AIModel(
            id=entry.model_path,
            name=model_name,
            provider=publisher,
            raw_model_id=entry.model_path,
        ),
        tokens=tokens,
        parsed_as=PARSED_AS_GOOGLE,
    )
