"""Cloud Audit Log reader for Vertex Gemini identity correlation.

Wraps ``google.cloud.logging.Client.list_entries()`` to fetch
``PredictionService.GenerateContent`` and ``StreamGenerateContent``
audit entries in a bounded time window. Parses each LogEntry's
``protoPayload`` (an ``AuditLog`` proto) into an ``AuditEntry`` pydantic
model that the join in ``event_source.py`` consumes.

Fields are declared via pydantic ``AliasPath`` so the audit-log
camelCase paths land directly on the model — no hand-rolled
``.get(...) or {}`` chain. Only fields the join needs are extracted;
everything else on the LogEntry stays untouched. Delegation info
(impersonation chain) preserves order (root at [0]) as a list of
``DelegationHop``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import AliasPath, BaseModel, ConfigDict, Field, field_validator
from slashid_ai_forwarder_core.events import GCPCredential

if TYPE_CHECKING:
    from google.cloud.logging import Client as LoggingClient
    from google.cloud.logging import LogEntry


# Google Front End appends a marker to every caller-supplied
# User-Agent, once per GFE hop (console traffic shows two). Verified
# empirically:
#   - The token is constant, not a negotiated encoding: every
#     ``Accept-Encoding`` we tried (gzip / br / deflate / identity /
#     multi-value / header omitted) still yields ``gzip(gfe)``. Hence
#     the loose ``[\w.-]+`` rather than a ``gzip`` literal — a future
#     variant is absorbed. No real client puts ``(gfe)`` in its UA.
#   - The leading comma is a separator, not part of the marker: a
#     request sending NO User-Agent logs a bare ``gzip(gfe)``. Hence
#     ``,?`` — without it that case survives the strip and we'd publish
#     GFE noise as the client's identity.
_GFE_UA_SUFFIX = re.compile(r"(?:,?[\w.-]+\(gfe\))+$")


class DelegationHop(BaseModel):
    """One hop in ``AuditEntry.delegation_chain``.

    Maps to one entry in
    ``AuditLog.authenticationInfo.serviceAccountDelegationInfo[]``.
    ``first_party_email`` reaches into ``firstPartyPrincipal.principalEmail``
    via a nested alias path; direct construction with the flat kwarg
    stays supported via ``populate_by_name``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    principal_subject: str | None = Field(default=None, validation_alias="principalSubject")
    first_party_email: str | None = Field(
        default=None,
        validation_alias=AliasPath("firstPartyPrincipal", "principalEmail"),
    )


class AuditEntry(BaseModel):
    """One Vertex Gemini audit log entry, projected to the fields the
    identity-correlation join reads.

    Validation happens directly against a ``google.cloud.logging.LogEntry``
    shape (with ``timestamp`` on the outer LogEntry and everything else
    under ``payload`` — the parsed ``AuditLog`` proto). Field aliases via
    ``AliasPath`` map camelCase audit-log paths to snake_case attributes.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    timestamp: datetime
    insert_id: str = Field(default="", validation_alias="insertId")
    resource_name: str = Field(
        default="",
        validation_alias=AliasPath("payload", "resourceName"),
    )
    method_name: str = Field(
        default="",
        validation_alias=AliasPath("payload", "methodName"),
    )
    effective_principal_email: str | None = Field(
        default=None,
        validation_alias=AliasPath("payload", "authenticationInfo", "principalEmail"),
    )
    effective_principal_subject: str | None = Field(
        default=None,
        validation_alias=AliasPath("payload", "authenticationInfo", "principalSubject"),
    )
    effective_oauth_client_id: str | None = Field(
        default=None,
        validation_alias=AliasPath("payload", "authenticationInfo", "oauthInfo", "oauthClientId"),
    )
    delegation_chain: list[DelegationHop] = Field(
        default_factory=list,
        validation_alias=AliasPath("payload", "authenticationInfo", "serviceAccountDelegationInfo"),
    )
    # Client that issued the call, as Vertex recorded it. Present on
    # every Cloud Audit Log entry (standard ``google.cloud.audit.AuditLog``
    # field, not method-specific) — e.g. ``"curl/8.5.0,gzip(gfe)"``.
    user_agent: str | None = Field(
        default=None,
        validation_alias=AliasPath("payload", "requestMetadata", "callerSuppliedUserAgent"),
    )

    @field_validator("user_agent", mode="after")
    @classmethod
    def _strip_gfe_suffix(cls, v: str | None) -> str | None:
        """Drop the Google Front End marker so consumers see the client's
        own User-Agent. Returns None if nothing survives the strip."""
        if v is None:
            return None
        return _GFE_UA_SUFFIX.sub("", v).strip() or None

    # gRPC status code on ``protoPayload.status``. Absent (whole
    # ``status`` object empty) on success, so the default of 0 covers
    # both "missing" and "explicitly OK". Non-zero → server-side error.
    status_code: int = Field(
        default=0,
        validation_alias=AliasPath("payload", "status", "code"),
    )

    @property
    def is_error(self) -> bool:
        """True when the audit entry represents a failed request.

        Cloud Audit Logs write ``protoPayload.status.code`` as a gRPC
        status: 0 (OK) is elided on success, non-zero on failure.
        """
        return self.status_code != 0

    @classmethod
    def from_log_entry(cls, entry: LogEntry) -> AuditEntry:
        """Build from a ``google.cloud.logging.LogEntry``.

        Wraps ``model_validate`` — the LogEntry has ``timestamp`` at the
        outer level and the AuditLog proto under ``payload`` (dict on the
        Cloud Logging v3+ Python client). ``AliasPath`` declarations on
        the fields pull each nested value directly.
        """
        return cls.model_validate(
            {
                "timestamp": entry.timestamp,
                "insertId": entry.insert_id,
                "payload": entry.payload or {},
            }
        )


def query_audit_entries(
    *,
    client: LoggingClient,
    project_id: str,
    region: str,
    ts_range: tuple[datetime, datetime],
) -> list[AuditEntry]:
    """Fetch Vertex Gemini audit entries in the given time range.

    Sync -- ``list_entries`` is a blocking generator; callers already
    live in the sync half of the forwarder (``BqEventSource.fetch``,
    itself sync). Audit entries per tick are usually <500 in count,
    which the API returns in one page in tens of ms.

    The filter pins to the caller's ``region`` via a substring match
    on ``protoPayload.resourceName`` (which carries
    ``/locations/<region>/`` as part of the path) so cross-region
    audit entries never reach Python — a single-region forwarder
    deployment can't correlate them anyway (the BQ dataset is
    region-scoped). Vertex audit logs don't populate
    ``resource.labels.location``; only ``method`` / ``project_id`` /
    ``service`` show up there. The per-row resource-name suffix check
    in ``_resolve_identity`` stays as belt-and-suspenders.

    ``datetime.isoformat()`` on our timezone-aware timestamps yields
    RFC 3339 output; Cloud Logging accepts both ``+00:00`` and ``Z``
    suffix forms.
    """
    ts_lo, ts_hi = ts_range
    # ``json.dumps`` on string values gives us the double-quoted,
    # JSON-escaped shape the Cloud Logging filter language expects —
    # future-proof against any interpolated value containing quotes
    # or backslashes (project IDs and regions are constrained
    # today, but the escape is free).
    return [
        AuditEntry.from_log_entry(e)
        for e in client.list_entries(
            resource_names=[f"projects/{project_id}"],
            filter_=(
                'resource.type="audited_resource" '
                'AND protoPayload.serviceName="aiplatform.googleapis.com" '
                'AND protoPayload.methodName:"generateContent" '
                f"AND resource.labels.project_id={json.dumps(project_id)} "
                f"AND protoPayload.resourceName:{json.dumps(f'/locations/{region}/')} "
                f"AND timestamp>={json.dumps(ts_lo.isoformat())} "
                f"AND timestamp<={json.dumps(ts_hi.isoformat())}"
            ),
            order_by="timestamp asc",
        )
    ]


def _credential_chain(a: AuditEntry) -> list[GCPCredential]:
    """Reconstruct the full credential chain from one audit entry:
    delegation hops (root at [0]) + effective principal (at [-1]).

    ``oauth_client_id`` from ``authenticationInfo.oauthInfo`` describes
    the token that authenticated THIS request — that's the effective
    credential (chain[-1]). For non-impersonated calls chain[0] ==
    chain[-1] so both interpretations coincide; for impersonated calls
    the value uniquely identifies the effective SA's OAuth flow (a
    numeric ID for SA-issued tokens, or the CLI's registered client ID
    for direct user calls). The audit log does not preserve the
    ROOT's OAuth flow across impersonation hops, so ``chain[0]``
    remains oauth-less."""
    chain = [
        GCPCredential(
            principal_email=hop.first_party_email,
            principal_subject=hop.principal_subject,
        )
        for hop in a.delegation_chain
    ]
    chain.append(
        GCPCredential(
            principal_email=a.effective_principal_email,
            principal_subject=a.effective_principal_subject,
            oauth_client_id=a.effective_oauth_client_id,
        )
    )
    return chain
