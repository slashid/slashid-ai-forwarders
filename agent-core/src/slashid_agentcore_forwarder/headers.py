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


def parse_baggage(value: str | None) -> Baggage:
    """``harness.id`` and ``session.id`` from a baggage string; properties are dropped."""
    found: dict[str, str] = {}
    for member in (value or "").split(","):
        key, sep, rest = member.split(";", 1)[0].partition("=")
        decoded = unquote(rest.strip())
        if sep and decoded and len(decoded) <= MAX_VALUE:
            found.setdefault(key.strip(), decoded)
    return Baggage(harness_id=found.get("harness.id"), session_id=found.get("session.id"))
