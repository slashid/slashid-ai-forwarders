"""Request headers and the W3C baggage that names the harness and session."""

from __future__ import annotations

from urllib.parse import unquote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from slashid_agentcore_forwarder.models import Lenient

MAX_VALUE = 256


class RequestHeaders(Lenient):
    """The headers we read; the rest, credentials included, are never kept."""

    user_agent: str | None = Field(None, validation_alias="user-agent")
    baggage: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _lowercase_keys(cls, raw: object) -> object:
        if isinstance(raw, dict):
            return {str(key).lower(): value for key, value in raw.items()}
        return raw

    @field_validator("user_agent", "baggage", mode="before")
    @classmethod
    def _text_only(cls, value: object) -> object:
        """A field of another type is dropped alone, not the whole model."""
        return value if isinstance(value, str) else None

    @field_validator("user_agent")
    @classmethod
    def _bounded(cls, value: str | None) -> str | None:
        return value if value is None or len(value) <= MAX_VALUE else None


def parse_headers(raw: JsonValue) -> RequestHeaders:
    try:
        return RequestHeaders.model_validate(raw)
    except ValidationError:
        return RequestHeaders()


class Baggage(BaseModel):
    model_config = ConfigDict(frozen=True)

    harness_id: str | None = None
    session_id: str | None = None

    def merged(self, other: Baggage) -> Baggage:
        """Each field from ``self``, else from ``other``."""
        return self.model_copy(
            update={
                "harness_id": self.harness_id or other.harness_id,
                "session_id": self.session_id or other.session_id,
            }
        )


def _usable(value: str) -> bool:
    """Non-empty, bounded, printable, and not a decoding failure (U+FFFD)."""
    return 0 < len(value) <= MAX_VALUE and value.isprintable() and "\ufffd" not in value


def parse_baggage(value: str | None) -> Baggage:
    """``harness.id`` and ``session.id`` from a baggage string; properties are dropped.

    The first member of a key decides it: a later duplicate never replaces it, even when the
    first was unusable.
    """
    found: dict[str, str | None] = {}
    for member in (value or "").split(","):
        key, sep, rest = member.split(";", 1)[0].partition("=")
        key = key.strip()
        if sep and key not in found:
            decoded = unquote(rest.strip())
            found[key] = decoded if _usable(decoded) else None
    return Baggage(harness_id=found.get("harness.id"), session_id=found.get("session.id"))
