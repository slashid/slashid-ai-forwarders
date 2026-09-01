"""Bedrock forwarder configuration.

Bedrock has no extra required env vars today; the subclass exists so
per-source additions (e.g. an offload bucket) have a home. ``load_config``
is cached so warm Lambda invocations reuse the parsed instance.
"""

from __future__ import annotations

from functools import cache

from slashid_ai_forwarder_core.config_base import BaseConfig


class Config(BaseConfig):
    """Bedrock-specific forwarder runtime configuration."""


@cache
def load_config() -> Config:
    """Load the forwarder config once per Lambda container."""
    return Config()  # pydantic-settings fills required fields from env
