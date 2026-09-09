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

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import AliasPath, BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from google.cloud.logging import Client as LoggingClient
    from google.cloud.logging import LogEntry


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

    @classmethod
    def from_log_entry(cls, entry: LogEntry) -> AuditEntry:
        """Build from a ``google.cloud.logging.LogEntry``.

        Wraps ``model_validate`` — the LogEntry has ``timestamp`` at the
        outer level and the AuditLog proto under ``payload`` (dict on the
        Cloud Logging v3+ Python client). ``AliasPath`` declarations on
        the fields pull each nested value directly.
        """
        return cls.model_validate({"timestamp": entry.timestamp, "payload": entry.payload or {}})


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

    The filter pins to the caller's ``region`` via
    ``resource.labels.location`` so cross-region audit entries never
    reach Python — a single-region forwarder deployment can't
    correlate them anyway (the BQ dataset is region-scoped). The
    per-row resource-name suffix check in ``_resolve_identity`` stays
    as belt-and-suspenders.

    ``datetime.isoformat()`` on our timezone-aware timestamps yields
    RFC 3339 output; Cloud Logging accepts both ``+00:00`` and ``Z``
    suffix forms.
    """
    ts_lo, ts_hi = ts_range
    return [
        AuditEntry.from_log_entry(e)
        for e in client.list_entries(
            resource_names=[f"projects/{project_id}"],
            filter_=(
                'resource.type="audited_resource" '
                'AND protoPayload.serviceName="aiplatform.googleapis.com" '
                'AND protoPayload.methodName:"generateContent" '
                f'AND resource.labels.project_id="{project_id}" '
                f'AND resource.labels.location="{region}" '
                f'AND timestamp>="{ts_lo.isoformat()}" '
                f'AND timestamp<="{ts_hi.isoformat()}"'
            ),
            order_by="timestamp asc",
        )
    ]
