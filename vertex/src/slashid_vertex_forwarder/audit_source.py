"""Cloud Audit Log reader for Vertex Gemini identity correlation.

Wraps ``google.cloud.logging.Client.list_entries()`` to fetch
``PredictionService.GenerateContent`` and ``StreamGenerateContent``
audit entries in a bounded time window. Parses each LogEntry's
protoPayload (an AuditLog proto) into an ``AuditEntry`` dataclass
that the join in ``event_source.py`` consumes.

Only fields the join needs are extracted; everything else on the
LogEntry stays untouched. Delegation info (impersonation chain)
preserves order (root -> effective) and is exposed as a list of
``DelegationHop``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DelegationHop:
    """One hop in ``AuditEntry.delegation_chain``.

    Maps to one entry in ``AuditLog.authenticationInfo.serviceAccountDelegationInfo[]``.
    """

    principal_subject: str | None = None
    first_party_email: str | None = None


@dataclass(frozen=True)
class AuditEntry:
    """One Vertex Gemini audit log entry, projected to the fields the
    identity-correlation join reads.

    - ``timestamp``: the LogEntry's ``timestamp`` (request receipt time,
      nanosecond precision).
    - ``resource_name`` / ``method_name``: from protoPayload, used to
      pin the candidate set to the right region + model + method.
    - ``effective_*`` fields: describe the effective principal
      (post-impersonation); come from ``authenticationInfo``.
    - ``delegation_chain``: preserves root -> intermediate hops
      of the impersonation chain, if any.
    """

    timestamp: datetime
    resource_name: str
    method_name: str
    effective_principal_email: str | None = None
    effective_principal_subject: str | None = None
    effective_oauth_client_id: str | None = None
    delegation_chain: list[DelegationHop] = field(default_factory=list)

    @classmethod
    def from_log_entry(cls, entry: Any) -> AuditEntry:
        """Extract the fields we need from a ``google.cloud.logging.LogEntry``.

        ``entry.payload`` is the parsed AuditLog proto as a dict-like.
        Malformed / missing fields degrade to None rather than raise --
        one broken entry shouldn't fail the whole tick.
        """
        payload: dict[str, Any] = getattr(entry, "payload", {}) or {}
        auth = payload.get("authenticationInfo") or {}

        oauth = auth.get("oauthInfo") or {}
        delegation_raw = auth.get("serviceAccountDelegationInfo") or []
        delegation = [
            DelegationHop(
                principal_subject=hop.get("principalSubject"),
                first_party_email=(hop.get("firstPartyPrincipal") or {}).get("principalEmail"),
            )
            for hop in delegation_raw
            if isinstance(hop, dict)
        ]

        return cls(
            timestamp=entry.timestamp,
            resource_name=str(payload.get("resourceName") or ""),
            method_name=str(payload.get("methodName") or ""),
            effective_principal_email=auth.get("principalEmail"),
            effective_principal_subject=auth.get("principalSubject"),
            effective_oauth_client_id=oauth.get("oauthClientId"),
            delegation_chain=delegation,
        )


_FILTER_TEMPLATE = (
    'resource.type="audited_resource" '
    'AND protoPayload.serviceName="aiplatform.googleapis.com" '
    'AND protoPayload.methodName:"generateContent" '
    'AND resource.labels.project_id="{project_id}" '
    'AND timestamp>="{ts_lo}" '
    'AND timestamp<="{ts_hi}"'
)


def query_audit_entries(
    *,
    client: Any,  # google.cloud.logging.Client
    project_id: str,
    region: str,  # noqa: ARG001 -- reserved for future per-region filters
    ts_range: tuple[datetime, datetime],
) -> list[AuditEntry]:
    """Fetch Vertex Gemini audit entries in the given time range.

    Sync -- ``list_entries`` is a blocking generator; callers already
    live in the sync half of the forwarder (``BqEventSource.fetch``,
    itself sync). Audit entries per tick are usually <500 in count,
    which the API returns in one page in tens of ms.

    Region is reserved for a future project-agnostic multi-region
    setup; the current filter is project-scoped which implicitly
    covers all regions in that project.
    """
    ts_lo, ts_hi = ts_range
    filter_str = _FILTER_TEMPLATE.format(
        project_id=project_id,
        ts_lo=_rfc3339(ts_lo),
        ts_hi=_rfc3339(ts_hi),
    )
    entries: list[AuditEntry] = []
    for e in client.list_entries(
        resource_names=[f"projects/{project_id}"],
        filter_=filter_str,
        order_by="timestamp asc",
    ):
        entries.append(AuditEntry.from_log_entry(e))
    return entries


def _rfc3339(ts: datetime) -> str:
    """RFC3339 timestamp with nanosecond precision, ``Z`` suffix. Cloud
    Logging filter expects this exact shape."""
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
