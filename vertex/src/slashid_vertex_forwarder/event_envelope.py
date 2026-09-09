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

    return EventEnvelope(
        request_id=entry.request_id,
        timestamp=entry.logging_time.isoformat(),
        identity_details=entry.identity_details,
        model=AIModel(
            id=entry.model_path,
            provider=_publisher_from_model_path(entry.model_path),
            raw_model_id=entry.model_path,
        ),
        tokens=tokens,
        parsed_as=PARSED_AS,
    )


def _publisher_from_model_path(model_path: str) -> str | None:
    """Extract the publisher segment from a Vertex model path.

    Vertex model paths follow ``publishers/<publisher>/models/<model>``.
    Phase 3.1 only exercises ``publishers/google/…`` (generateContent),
    but Model Garden hosts anthropic, meta, mistralai, and others via
    rawPredict — landing in phase 3.3+. Parse now so the provider field
    stays honest when those normalizers come online.
    """
    parts = model_path.split("/")
    if len(parts) >= 2 and parts[0] == "publishers":
        return parts[1] or None
    return None
