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

import re

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
        parsed_as=PARSED_AS,
    )
