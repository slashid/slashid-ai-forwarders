"""Cold-start configuration loading.

Reads non-secret config from environment variables and the SlashID admin
JWT (or API key) from SSM Parameter Store. Values are cached at module
scope so warm invocations reuse them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass(frozen=True, slots=True)
class Config:
    """Forwarder runtime configuration."""

    endpoint: str
    org_id: str
    admin_token: str
    identity_source_type: str
    request_timeout_seconds: float
    max_retries: int


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


def _load_ssm_parameter(name: str) -> str:
    """Fetch and decrypt an SSM SecureString parameter."""
    # boto3 ships in the Lambda runtime; import lazily so tests don't need it.
    import boto3

    client = boto3.client("ssm")
    resp = client.get_parameter(Name=name, WithDecryption=True)
    value = resp["Parameter"]["Value"]
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"SSM parameter {name} is empty")
    return value


@cache
def load_config() -> Config:
    """Load the forwarder config once per Lambda container."""
    endpoint = _require("SLASHID_ENDPOINT").rstrip("/")
    org_id = _require("SLASHID_ORG_ID")
    token_param = _require("SLASHID_TOKEN_SSM_PARAMETER")
    admin_token = _load_ssm_parameter(token_param)

    return Config(
        endpoint=endpoint,
        org_id=org_id,
        admin_token=admin_token,
        identity_source_type=os.environ.get("SLASHID_IDENTITY_SOURCE_TYPE", "manual_import"),
        request_timeout_seconds=float(os.environ.get("SLASHID_REQUEST_TIMEOUT_SECONDS", "10")),
        max_retries=int(os.environ.get("SLASHID_MAX_RETRIES", "3")),
    )
