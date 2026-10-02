"""Base configuration for SlashID AI forwarders via pydantic-settings.

Subclasses (one per forwarder) may add per-source fields — e.g. Bedrock's
optional S3 offload bucket — but the shared surface below is enough to
POST to the SlashID NHI subgraph.

All values come from environment variables with the ``SLASHID_`` prefix.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ENDPOINT = "https://api.slashid.com"


class BaseConfig(BaseSettings):
    """Shared runtime configuration for any AI-forwarder Lambda/service."""

    model_config = SettingsConfigDict(
        env_prefix="SLASHID_",
        frozen=True,
        extra="ignore",
    )

    # The production API; override for another environment.
    endpoint: str = Field(DEFAULT_ENDPOINT, min_length=1)
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
    # ``round`` hashes only the messages the model consumed; ``session`` the
    # whole transcript before the response.
    input_scope: Literal["session", "round"] = "round"
    # How many rounds ``recent_round_hashes`` lists, own round included.
    round_link_depth: int = Field(10, ge=1)
    request_timeout_seconds: float = 10.0
    max_retries: int = 3

    @field_validator("endpoint")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # Customer-pasted base URLs often end in "/"; the sink builds
        # `endpoint + "/ip/nhi/events/..."` so a trailing slash produces a
        # double slash. Normalise at the boundary.
        return v.rstrip("/")
