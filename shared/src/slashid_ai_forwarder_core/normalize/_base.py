"""Shared pydantic base classes used by vendor-format schemas.

``_LenientModel`` is used by every schema class; ``_StrictModel`` is used
only for Converse content-block variants where field-presence acts as
the discriminator (pydantic's smart-union picks the strict variant whose
key is present).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _LenientModel(BaseModel):
    """Tolerates unknown fields — safe against vendor evolution."""

    model_config = ConfigDict(extra="ignore")


class _StrictModel(BaseModel):
    """Rejects unknown fields — used for key-tagged unions where the
    presence of a specific key determines which variant validates."""

    model_config = ConfigDict(extra="forbid")
