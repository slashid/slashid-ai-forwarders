"""The ``identity_details`` of each surface (spec 4.2)."""

from __future__ import annotations

import re
from typing import Protocol

from slashid_ai_forwarder_core.events import AWSIdentityDetails

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.headers import Baggage
from slashid_agentcore_forwarder.roles import RateLimited

_HARNESS_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class RoleLookup(Protocol):
    async def harness_role(self, harness_arn: str) -> str | None: ...
    async def gateway_role(self, gateway_arn: str) -> str | None: ...
    async def claimed_harness_role(
        self, harness_arn: str, *, fresh: bool = False
    ) -> str | None: ...


async def harness_identity(roles: RoleLookup, harness_arn: str) -> AWSIdentityDetails:
    """The Harness and its execution role; the role is left out when the lookup fails."""
    return AWSIdentityDetails(
        agent_arn=harness_arn, principal_arn=await roles.harness_role(harness_arn)
    )


async def gateway_identity(
    roles: RoleLookup, *, gateway_arn: str | None, principal: str | None, baggage: Baggage
) -> AWSIdentityDetails | None:
    """``None`` when there is neither a Gateway ARN nor a principal to send.

    ``principal`` is the caller's ``awsPrincipalArn``; without it (a JWT Gateway) the Gateway's own
    role stands in, and the baggage has nothing to be verified against.
    """
    if gateway_arn is None and principal is None:
        return None
    agent_arn = None
    if principal is None and gateway_arn is not None:
        principal = await roles.gateway_role(gateway_arn)
        if principal is None:
            logs.gateway_role_unavailable()
        if baggage.harness_id is not None:
            logs.baggage_unverified("no_principal")
    elif baggage.harness_id is not None:
        agent_arn = await _verified_agent(roles, gateway_arn, principal, baggage.harness_id)
    return AWSIdentityDetails(agent_arn=agent_arn, principal_arn=principal, gateway_arn=gateway_arn)


async def _verified_agent(
    roles: RoleLookup, gateway_arn: str | None, principal: str | None, harness_id: str
) -> str | None:
    """The Harness ARN built from a claimed id, when that Harness runs with the caller's role."""
    parts = (gateway_arn or "").split(":", 5)
    if len(parts) < 6 or not all(parts[:5]):
        logs.baggage_unverified("no_gateway_arn")
        return None
    if not _HARNESS_ID.fullmatch(harness_id):
        logs.baggage_unverified("invalid_id")
        return None
    partition, region, account = parts[1], parts[3], parts[4]
    claimed = f"arn:{partition}:bedrock-agentcore:{region}:{account}:harness/{harness_id}"
    try:
        role = await roles.claimed_harness_role(claimed)
        if role is not None and role != principal:
            role = await roles.claimed_harness_role(claimed, fresh=True)
    except RateLimited:
        logs.baggage_unverified("rate_limited")
        return None
    if role is None:
        logs.baggage_unverified("lookup_failed")
    elif role != principal:
        logs.baggage_unverified("mismatch")
    else:
        return claimed
    return None
