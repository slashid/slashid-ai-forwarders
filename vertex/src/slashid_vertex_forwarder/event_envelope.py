"""BQ row (``Entry``) → shared ``EventEnvelope``.

Mirrors ``bedrock/event_envelope.py`` — the vendor-specific half of the
event build. Reads directly from the ``Entry`` (which itself wraps one
BQ request-response logging row); produces a vendor-neutral
``EventEnvelope`` which the shared pure
``build_event_from_normalized`` turns into an
``AIInvocationObservedV1``.

Tokens live inside ``response_body.usageMetadata`` — parsed there
rather than off the record's top-level fields (Bedrock reads
top-level; Vertex hides them inside the response payload).

V1 punts on caller identity: BQ rows carry no principal (see the
2026-09-04 POC findings). Every event ships with
``identity_details = {"kind": "gcp"}`` until a later phase adds
audit-log correlation.
"""

from __future__ import annotations

from slashid_ai_forwarder_core.events import (
    AIInvocationTokens,
    AIModel,
    EventEnvelope,
    GCPIdentityDetails,
)
from slashid_ai_forwarder_core.normalize.gemini.stop_reasons import (
    STOP_REASONS,
)

from .event_source import Entry

# Kept as a module-level constant so tests and downstream consumers can
# pattern-match on the exact string. Convention (see events.py:246):
# ``<vendor>-<vendor_model_family>-<shape>``.
PARSED_AS = "vertex-gemini-generate"


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

    # candidates[0].finishReason drives the wire stop_reason. Empty
    # candidates list is a valid SAFETY-block shape; fall through to
    # "unknown" in that case.
    stop_reason = None
    if entry.response_body.candidates:
        raw = entry.response_body.candidates[0].finishReason
        stop_reason = STOP_REASONS.get(raw or "", "unknown")

    return EventEnvelope(
        request_id=entry.request_id,
        timestamp=entry.logging_time.isoformat(),
        identity_details=GCPIdentityDetails(),
        model=AIModel(
            id=entry.model_path,
            provider="google",
            raw_model_id=entry.model_path,
        ),
        tokens=tokens,
        parsed_as=PARSED_AS,
        stop_reason=stop_reason,
    )
