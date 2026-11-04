"""The base of every payload model, and the types that keep a payload as it came."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, JsonValue, SkipValidation


class Lenient(BaseModel):
    """Unknown fields are ignored and a validation error never echoes the payload. Strings are
    kept as they came: the Gateway echoes them back."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


# JSON from the platform, held as it came: pydantic neither copies it, strips its strings nor
# bounds its depth (``JsonValue`` validation fails past ~250 levels).
Raw = SkipValidation[JsonValue]


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("blank")
    return value


# A name or id: non-empty and not whitespace only, and returned unchanged.
NonBlank = Annotated[str, AfterValidator(_not_blank)]
