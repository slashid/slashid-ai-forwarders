"""The base of every payload model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Lenient(BaseModel):
    """Unknown fields are ignored, a validation error never echoes the payload, and a name made
    of whitespace is empty."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True, str_strip_whitespace=True)
