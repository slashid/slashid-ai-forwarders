"""Runtime configuration for the Inference hooks receiver."""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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
    # POST {endpoint}/nhi/ai/preflight. Off until that endpoint ships.
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
    # sk-ant-api01-…. Setting it enables the compliance readers; the
    # readers chunk adds the rest of their configuration. It is read on
    # the request path for one reason: a record may only wait for
    # attachment digests when something exists to deliver them.
    compliance_key: str | None = None
    # The key can read every linked organization while the hook's tenant
    # binding is per organization, so the readers filter to this one. It
    # equals the frame's `tenant_id`. Not a query parameter: both
    # listings reject `organization_uuid`, so the filter runs over the
    # rows a listing returns.
    organization_uuid: str | None = None
    # How far behind now the `updated_at.gte` bound sits, and the initial
    # watermark on a cold start — never a full backfill.
    poll_lag_seconds: int = 120
    # Bounds one tick against the 600 rpm shared with the sync adapter.
    # The local-session listing cannot be ordered, so a tick that hits
    # this cap leaves the *oldest* sessions untouched and must not
    # advance its watermark.
    max_sessions_per_tick: int = 200
    # `md5` takes the digest the file listing already carries and makes
    # no extra request. `full` downloads the stored bytes for sha1 and
    # sha256, which OneDrive, SharePoint and Drive resources need.
    attachment_hashing: Literal["md5", "full"] = "md5"
    # Under `full`, the largest attachment worth downloading. Decided
    # from the listing's `size_bytes` *before* any fetch: an oversized
    # file is never started, and falls back to the listing's md5 rather
    # than to no digest. A ranged read yields a snippet, never a digest.
    max_attachment_fetch_bytes: int = 10 * 1024 * 1024
    # One document per feed, in its own collection: a watermark is a
    # different lifetime from a pending record, and the pending
    # collection carries a TTL policy that would delete these.
    checkpoint_collection: str = "anthropic_checkpoints"
    # How far from a message a pending record may sit and still be its
    # soft-join candidate. A knob because it decides the answer:
    # measured, the nearest record sat 0.3 to 6.9 s away with the runner-up
    # at least 6.1 s further, so at ±15 s three attachment rounds resolve
    # to exactly one candidate — and at ±60 s one of them gains a second
    # and abstains.
    soft_join_window_seconds: int = 15
    # The project holding Firestore; vertex/ has the same field. Required:
    # this chunk builds the client, and ``project=None`` is a client that
    # talks to nothing.
    gcp_project_id: str = Field(..., min_length=1)
    # The named database, as vertex/ names its own slashid-vertex rather
    # than using (default).
    firestore_database: str = "slashid-anthropic"
    # Collection holding pending records and their tombstones.
    pending_collection: str = "anthropic_pending"
    # Deadline before an unsettled record is pushed as it stands.
    join_wait_seconds: int = 3_600
    # How long a pushed record's tombstone suppresses a late reader's
    # duplicate. Must exceed JOIN_WAIT + POLL_LAG + one tick; the
    # assertion lands with the tick cadence in the deploy chunk.
    tombstone_ttl_seconds: int = 7_200
    # Bounds `due` so one tick cannot stall behind a backlog.
    max_flushes_per_tick: int = 500
    # The service account whose OIDC token POST /tick accepts. Unset
    # refuses every tick, which is the right way round: Cloud Run cannot
    # scope an invoker to one path, so on a service the hook can reach,
    # this check is the only thing guarding the route.
    tick_service_account: str | None = None
    # The audience that token must carry, when the deployment can name it.
    # The service's own URI is not available to the Terraform that sets
    # this service's environment, so it may be left unset: the signature
    # and the service account still authorize, and Cloud Run enforces the
    # audience itself wherever the service is not public.
    tick_audience: str | None = None
    # The Cloud Scheduler cadence, declared here as a number: the cron
    # string in Terraform is not something this process can compare
    # against ``tombstone_ttl_seconds``. The module derives the cron from
    # it, so this is the input and the cron is the derivation.
    tick_interval_seconds: int = 300

    # Hex digests that deny. Exists so a test tenant can drive a real
    # denial without depending on the graph having anything tagged
    # sensitive: the composition, the deny reason, the guardrail stamp,
    # the `deny:` address and Reader A's join
    # onto it are all unreachable. Runs alongside preflight rather than
    # instead of it, so enabling the real endpoint later changes nothing.
    #
    # Content-addressed, unlike CAPTURE_DENY_MARKER: a literal token is
    # tripped by anyone who merely quotes it, which has wedged a working
    # session before now. Test tenants only.
    mock_denied_hashes: str = ""

    @field_validator(
        "compliance_key",
        "organization_uuid",
        "capture_bucket",
        "capture_deny_marker",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # Terraform sets every env var it manages, ``""`` where a
        # deployment left it out, and pydantic-settings does not treat
        # ``""`` as unset. ``capture_bucket`` and ``capture_deny_marker``
        # are not module variables — they are here because a hand-edited
        # revision can leave them empty just as easily.
        return v or None

    @property
    def denied_hashes(self) -> tuple[str, ...]:
        """Lowercased, so a digest pasted from a tool that prints
        uppercase still matches the lowercase hex every source gives us."""
        return tuple(h.strip().lower() for h in self.mock_denied_hashes.split(",") if h.strip())

    @property
    def compliance_enabled(self) -> bool:
        return bool(self.compliance_key)

    @property
    def signing_secrets(self) -> list[str]:
        return [s.strip() for s in self.hook_signing_secret.split(",") if s.strip()]

    @property
    def hook_enabled(self) -> bool:
        """The signing secret enables the hook. ``compliance_enabled`` is its
        counterpart and is already defined above.
        """
        return bool(self.signing_secrets)

    @model_validator(mode="after")
    def _check_capabilities(self) -> Config:
        # At least one credential, or there is nothing to run. The signing
        # secret is required only when the hook is the capability in use:
        # compliance-only needs none, and demanding one made that deployment
        # impossible to start.
        if not self.hook_enabled and not self.compliance_enabled:
            raise ValueError(
                "no capability configured: set SLASHID_HOOK_SIGNING_SECRET for the hook, "
                "SLASHID_COMPLIANCE_KEY for the readers"
            )
        if self.compliance_enabled and not self.organization_uuid:
            raise ValueError("SLASHID_ORGANIZATION_UUID is required with SLASHID_COMPLIANCE_KEY")
        floor = self.join_wait_seconds + self.poll_lag_seconds + self.tick_interval_seconds
        if self.tombstone_ttl_seconds <= floor:
            raise ValueError(
                f"SLASHID_TOMBSTONE_TTL_SECONDS ({self.tombstone_ttl_seconds}) must exceed "
                f"JOIN_WAIT + POLL_LAG + one tick ({floor}): a reader arriving after its own "
                "tombstone expired re-emits the invocation"
            )
        return self


@cache
def load_config() -> Config:
    """Load once per process."""
    return Config()
