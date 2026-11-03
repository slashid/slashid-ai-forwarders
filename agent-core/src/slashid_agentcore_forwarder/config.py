"""AgentCore forwarder configuration. All settings use the ``SLASHID_`` prefix."""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import field_validator
from slashid_ai_forwarder_core.config_base import BaseConfig


class Config(BaseConfig):
    harness_hooks_enabled: bool = True
    gateway_hooks_enabled: bool = True
    # Answer when a check gives no answer. Allow: refusing to answer is self-inflicted downtime.
    verdict_fail_mode: Literal["allow", "deny"] = "allow"
    # Bound on one check, under the hook timeout and the Lambda timeout (10 s each).
    verdict_budget_ms: int = 3_500
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
