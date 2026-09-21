"""Media-type parsing with tolerant preprocessing.

Wraps ``pydantic_extra_types.mime_types.MimeType`` with tolerant
preprocessing: strips parameters (``text/plain; charset=utf-8`` →
``text/plain``) and whitespace, returns ``None`` on empty or on a value
the IANA registry doesn't know. Validation runs through a TypeAdapter
because ``MimeType`` is a plain ``str`` subclass whose registry check
lives in a pydantic validator — constructing one directly never raises,
it just defers the failure to the first model that holds it.
"""

from __future__ import annotations

import logging

from pydantic import TypeAdapter, ValidationError
from pydantic_extra_types.mime_types import MimeType

log = logging.getLogger(__name__)

_MIME = TypeAdapter(MimeType)


def parse_media_type(raw: str | None) -> MimeType | None:
    """Parse ``raw`` into a ``MimeType``; return ``None`` on empty input.

    Handles the two common wire quirks:
      - parameters (``"text/plain; charset=utf-8"`` — RFC 6838 §4.3) —
        stripped before validation
      - surrounding whitespace — stripped

    Unregistered values (a bare ``"txt"`` from a compliance file listing,
    a vendor-invented type) return ``None``. Returning the raw string
    would only move the failure to ``NormalizedContent.media_type``,
    which validates through the same registry and raises there.
    """
    if not raw:
        return None
    base = raw.split(";", 1)[0].strip()
    if not base:
        return None
    try:
        return _MIME.validate_python(base)
    except ValidationError:
        log.debug("unrecognized media type: %r", raw)
        return None
