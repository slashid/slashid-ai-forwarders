"""AgentCore forwarder configuration. All settings use the ``SLASHID_`` prefix."""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict
from slashid_ai_forwarder_core.config_base import BaseConfig


class Config(BaseConfig):
    model_config = SettingsConfigDict(hide_input_in_errors=True)

    harness_hooks_enabled: bool = True
    gateway_hooks_enabled: bool = True
    # Answer when a check gives no answer. Allow: refusing to answer is self-inflicted downtime.
    verdict_fail_mode: Literal["allow", "deny"] = "allow"
    # Bound on one check. Role lookups before it take up to 2 s, so the worst case is 2 s + this +
    # overhead, under the hook and Lambda timeouts (10 s each).
    verdict_budget_ms: int = Field(3_500, ge=100, le=6_500)
    # Evaluate and log, answer allow.
    shadow_mode: bool = False

    @field_validator("include_raw_content")
    @classmethod
    def _never_raw(cls, _: bool) -> bool:
        """Connector files carry no text."""
        return False


@cache
def load_config() -> Config:
    """Load the forwarder config once per Lambda container."""
    return Config()
