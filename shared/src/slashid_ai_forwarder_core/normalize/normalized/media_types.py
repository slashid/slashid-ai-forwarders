"""Media-type parsing with tolerant preprocessing.

Wraps ``pydantic_extra_types.mime_types.MimeType`` with tolerant
preprocessing: strips parameters (``text/plain; charset=utf-8`` →
``text/plain``) and whitespace, returns ``None`` on empty. Does not
enforce registry validity — the underlying ``MimeType`` accepts any
well-formed value, and callers already treat MIME strings as loose (the
wire model is ``str | None``).
"""

from __future__ import annotations

import logging

from pydantic_extra_types.mime_types import MimeType

log = logging.getLogger(__name__)


def parse_media_type(raw: str | None) -> MimeType | None:
    """Parse ``raw`` into a ``MimeType``; return ``None`` on empty input.

    Handles the two common wire quirks:
      - parameters (``"text/plain; charset=utf-8"`` — RFC 6838 §4.3) —
        stripped before construction
      - surrounding whitespace — stripped

    Does not enforce IANA-registry validity; ``MimeType`` in the pinned
    ``pydantic-extra-types`` release is a permissive ``str`` subclass.
    """
    if not raw:
        return None
    base = raw.split(";", 1)[0].strip()
    if not base:
        return None
    try:
        return MimeType(base)
    except ValueError:
        log.debug("unrecognized media type: %r", raw)
        return None
