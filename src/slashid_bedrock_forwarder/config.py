"""Cold-start configuration via pydantic-settings.

All values come from environment variables with the `SLASHID_` prefix.
`load_config()` is cached so warm invocations reuse the parsed instance.

The push token is the SlashID connection's event-streaming token;
CloudFormation sets it from a `NoEcho` parameter, and Lambda env vars
are encrypted at rest with a KMS key.
"""

from __future__ import annotations

from functools import cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Forwarder runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SLASHID_",
        frozen=True,
        extra="ignore",
    )

    endpoint: str = Field(..., min_length=1)
    org_id: str = Field(..., min_length=1)
    connection_id: str = Field(..., min_length=1)
    push_token: str = Field(..., min_length=1)
    identity_source_type: str = "manual_import"
    request_timeout_seconds: float = 10.0
    max_retries: int = 3


@cache
def load_config() -> Config:
    """Load the forwarder config once per Lambda container."""
    return Config()  # pydantic-settings fills required fields from env
