"""Cold-start configuration via pydantic-settings.

All values come from environment variables with the `SLASHID_` prefix.
`load_config()` is cached so warm invocations reuse the parsed instance.

The push token is the SlashID connection's event-streaming token;
CloudFormation sets it from a `NoEcho` parameter, and Lambda env vars
are encrypted at rest with a KMS key.
"""

from __future__ import annotations

from functools import cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Forwarder runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="SLASHID_",
        frozen=True,
        extra="ignore",
    )

    endpoint: str = Field(..., min_length=1)
    push_token: str = Field(..., min_length=1)
    # When true, the full input/output JSON bodies travel in
    # AIInvocationContent.redacted_text. Off by default — hash + mime +
    # byte length still go out so the server can dedup / correlate.
    include_raw_content: bool = False
    # Maximum characters of raw content stored in redacted_text / redacted_content.
    # Excess is elided with middle truncation ("first…last") to preserve both
    # the header and the tail of large bodies. Applies to input, output, and
    # accessed file contents when include_raw_content is True.
    max_content_size: int = 100_000
    request_timeout_seconds: float = 10.0
    max_retries: int = 3

    @field_validator("endpoint")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # Customer-pasted base URLs often end in "/"; the sink builds
        # `endpoint + "/nhi/events/..."` so a trailing slash produces a
        # double slash. Normalise at the boundary.
        return v.rstrip("/")


@cache
def load_config() -> Config:
    """Load the forwarder config once per Lambda container."""
    return Config()  # pydantic-settings fills required fields from env
