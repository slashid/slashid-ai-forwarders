"""Runtime configuration for the Inference hooks receiver."""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import model_validator
from slashid_ai_forwarder_core.config_base import BaseConfig


class Config(BaseConfig):
    """Inherits SLASHID_ENDPOINT, SLASHID_PUSH_TOKEN, SLASHID_INCLUDE_RAW_CONTENT,
    SLASHID_MAX_CONTENT_SIZE, SLASHID_REQUEST_TIMEOUT_SECONDS and
    SLASHID_MAX_RETRIES from BaseConfig.
    """

    # Comma-separated, any length. A rotation needs at least two live at
    # once, since requests signed with the previous secret keep arriving
    # for about a minute after the cutover; ``verify`` tries every entry,
    # so more than two is allowed and costs one failed HMAC each.
    hook_signing_secret: str = ""
    # Escape hatch for an organization that enabled hooks before signing
    # secrets were required. Default false: unsigned requests get 401.
    hook_allow_unsigned: bool = False
    # The Go policy receiver (POST /ai-access/<id>). None skips the check.
    policy_url: str | None = None
    # POST {endpoint}/ip/nhi/ai/preflight. Off until that endpoint ships.
    preflight_enabled: bool = False
    # Answer when a check fails or comes back unverified. Allow by default:
    # refusing to answer is self-inflicted downtime, and the customer has
    # Anthropic-side failure handling for strictness.
    verdict_fail_mode: Literal["allow", "deny"] = "allow"
    # Both checks run concurrently under this budget, kept under
    # Anthropic's configured verdict timeout (5 s by default).
    verdict_budget_ms: int = 3_500
    # Bounds the background push so a hung sink cannot pin an instance.
    # It never delays the verdict.
    push_budget_ms: int = 2_000
    # Our own shadow mode, named after claude.ai's `shadow_mode` and
    # independent of it: when either is on, nothing is blocked. On by
    # default, so a fresh deployment observes before it enforces.
    shadow_mode: bool = True
    # Request body cap. Cloud Run's HTTP/1 limit is 32 MiB.
    max_body_bytes: int = 32 * 1024 * 1024
    # When set, every frame is written raw to this GCS bucket for
    # protocol study. Test tenants only.
    capture_bucket: str | None = None
    # When set and found in a frame's raw body, the frame is denied. Lets a
    # test tenant observe what a post-denial round looks like.
    capture_deny_marker: str | None = None

    @property
    def signing_secrets(self) -> list[str]:
        return [s.strip() for s in self.hook_signing_secret.split(",") if s.strip()]

    @model_validator(mode="after")
    def _check_signing(self) -> Config:
        if not self.signing_secrets and not self.hook_allow_unsigned:
            raise ValueError("SLASHID_HOOK_SIGNING_SECRET is required unless HOOK_ALLOW_UNSIGNED")
        if self.hook_allow_unsigned and self.policy_url:
            raise ValueError("HOOK_ALLOW_UNSIGNED cannot be combined with POLICY_URL")
        return self


@cache
def load_config() -> Config:
    """Load once per process."""
    return Config()
