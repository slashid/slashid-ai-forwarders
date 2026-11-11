"""The ``identity_details`` and the Harness of each surface (spec 4.2)."""

from __future__ import annotations

import re
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from slashid_ai_forwarder_core.events import AIAgentDetails, AWSIdentityDetails

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.headers import Baggage
from slashid_agentcore_forwarder.roles import HarnessDetails, RateLimited

# botocore's pattern for GetHarness ``harnessId``: an id it would reject never costs a lookup.
HARNESS_ID = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,39}-[a-zA-Z0-9]{10}")


PROVIDER = "aws-agentcore"


class RoleLookup(Protocol):
    async def harness_role(self, harness_arn: str) -> str | None: ...
    def harness_details(self, harness_arn: str) -> HarnessDetails | None: ...
    async def gateway_role(self, gateway_arn: str) -> str | None: ...
    async def claimed_harness_role(
        self, harness_arn: str, *, fresh: bool = False
    ) -> str | None: ...


class Caller(BaseModel):
    """Who a call is for: the identity it runs as and the Harness, when one is known."""

    model_config = ConfigDict(frozen=True)

    identity: AWSIdentityDetails
    agent: AIAgentDetails | None


async def harness_identity(roles: RoleLookup, harness_arn: str) -> Caller:
    """The Harness and its execution role; the role is left out when the lookup fails."""
    principal = await roles.harness_role(harness_arn)
    details = roles.harness_details(harness_arn)
    return Caller(
        identity=AWSIdentityDetails(principal_arn=principal),
        agent=AIAgentDetails(
            id=harness_arn,
            provider=PROVIDER,
            raw_agent_id=harness_arn.rsplit("/", 1)[-1],
            name=details.name if details else None,
            version=details.version if details else None,
        ),
    )


async def gateway_identity(
    roles: RoleLookup, *, gateway_arn: str | None, principal: str | None, baggage: Baggage
) -> Caller:
    """``principal`` is the caller's ``awsPrincipalArn``; without it (a JWT Gateway) the Gateway's
    own role stands in, and the baggage has nothing to be verified against."""
    agent = None
    if principal is None and gateway_arn is not None:
        principal = await roles.gateway_role(gateway_arn)
        if principal is None:
            logs.gateway_role_unavailable()
        if baggage.harness_id is not None:
            logs.baggage_unverified("no_principal")
    elif baggage.harness_id is not None:
        agent = await _verified_harness(roles, gateway_arn, principal, baggage.harness_id)
    return Caller(
        identity=AWSIdentityDetails(principal_arn=principal, gateway_arn=gateway_arn), agent=agent
    )


async def _verified_harness(
    roles: RoleLookup, gateway_arn: str | None, principal: str | None, harness_id: str
) -> AIAgentDetails | None:
    """The Harness a baggage names, when it runs with the caller's role."""
    parts = (gateway_arn or "").split(":", 5)
    if len(parts) < 6 or not all(parts[:5]):
        logs.baggage_unverified("no_gateway_arn")
        return None
    if not HARNESS_ID.fullmatch(harness_id):
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
        return AIAgentDetails(id=claimed, provider=PROVIDER, raw_agent_id=harness_id)
    return None
