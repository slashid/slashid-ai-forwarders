"""Runtime configuration for the ChatGPT Work hook and the upload reader."""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import field_validator, model_validator
from slashid_ai_forwarder_core.config_base import BaseConfig

COMPLIANCE_BASE_URL = "https://api.chatgpt.com/v1"


class Config(BaseConfig):
    """Inherits SLASHID_ENDPOINT, SLASHID_PUSH_TOKEN (the OpenAI connection's
    event-streaming token), SLASHID_INCLUDE_RAW_CONTENT and the rest from
    BaseConfig."""

    # This service's origin as ChatGPT reaches it. The MCP resource is
    # ``<public_url>/mcp``; it is what the protected-resource metadata names
    # and the default audience a token must carry.
    public_url: str | None = None
    # The customer's IdP, which issues the per-user tokens the MCP connector
    # presents. Unset refuses every hook call: a hook that authenticates
    # nobody cannot name a user, and a guess would attribute a prompt to the
    # wrong person.
    oidc_issuer: str | None = None
    # The audience the token must carry. Defaults to the MCP resource URL,
    # which is what an RFC 8707 resource indicator makes the IdP stamp.
    oidc_audience: str | None = None
    # Discovered from ``<issuer>/.well-known/openid-configuration`` when unset.
    oidc_jwks_url: str | None = None
    # The claim holding the user's email: ``email`` for most IdPs, ``upn``
    # or ``preferred_username`` for some Entra tenants.
    oidc_user_claim: str = "email"

    # The answer when there is no verdict: the user is not in the directory,
    # or preflight failed. Allow by default, as the Anthropic hook does:
    # refusing every prompt is self-inflicted downtime, and a hook error
    # already fails open on OpenAI's side.
    verdict_fail_mode: Literal["allow", "deny"] = "allow"
    # Kept under the handler's ``timeout`` in requirements.toml.
    verdict_budget_seconds: float = 8.0

    # An Admin API key scoped to the workspace, with the users read and
    # ``chatgpt.enterprise.compliance_logs_platform.conversation_message.read``.
    # The directory needs it to turn an email into a ``user-…`` id, and the
    # upload reader needs it to read anything.
    compliance_key: str | None = None
    workspace_id: str | None = None
    compliance_base_url: str = COMPLIANCE_BASE_URL
    compliance_timeout_seconds: float = 60.0
    # How long the email → user map is trusted before it is listed again.
    directory_ttl_seconds: int = 600
    # A miss lists again, but no more often than this.
    directory_miss_refresh_seconds: int = 60
    # Bounds one tick: the files listed and downloaded before the watermark
    # is saved and the tick returns.
    max_log_files_per_tick: int = 50

    platform: Literal["gcp", "local"] = "local"
    project_id: str | None = None
    data_dir: str | None = None
    database: str = "slashid-openai"
    checkpoint_collection: str = "openai_checkpoints"
    # The service account whose OIDC token POST /tick accepts; unset refuses
    # every tick.
    tick_principal: str | None = None
    tick_audience: str | None = None
    tick_interval_seconds: int = 300

    @field_validator(
        "public_url",
        "oidc_issuer",
        "oidc_audience",
        "oidc_jwks_url",
        "compliance_key",
        "workspace_id",
        "data_dir",
        "project_id",
        "tick_principal",
        "tick_audience",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # Terraform sets every variable it manages, ``""`` where unset.
        return v or None

    @field_validator("public_url", "oidc_issuer", "compliance_base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    @property
    def resource_url(self) -> str | None:
        return f"{self.public_url}/mcp" if self.public_url else None

    @property
    def audience(self) -> str | None:
        return self.oidc_audience or self.resource_url

    @property
    def compliance_enabled(self) -> bool:
        return bool(self.compliance_key and self.workspace_id)

    @model_validator(mode="after")
    def _check(self) -> Config:
        if self.platform == "gcp" and not self.project_id:
            raise ValueError("SLASHID_PROJECT_ID is required on the gcp platform")
        if bool(self.compliance_key) != bool(self.workspace_id):
            raise ValueError("SLASHID_COMPLIANCE_KEY and SLASHID_WORKSPACE_ID go together")
        if self.oidc_issuer and not self.audience:
            raise ValueError(
                "SLASHID_OIDC_ISSUER needs SLASHID_PUBLIC_URL or SLASHID_OIDC_AUDIENCE"
            )
        return self


@cache
def load_config() -> Config:
    return Config()
