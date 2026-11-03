import json
import logging

import pytest
from slashid_ai_forwarder_core.events import AWSIdentityDetails

from slashid_agentcore_forwarder.headers import Baggage
from slashid_agentcore_forwarder.identity import gateway_identity, harness_identity
from slashid_agentcore_forwarder.roles import RateLimited

LOGGER = "slashid_agentcore_forwarder"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/h-1"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:eu-west-1:210987654321:gateway/g-1"
CLAIMED_ARN = "arn:aws:bedrock-agentcore:eu-west-1:210987654321:harness/h-2"
ROLE = "arn:aws:iam::210987654321:role/exec"
OTHER_ROLE = "arn:aws:iam::210987654321:role/other"


class FakeRoles:
    def __init__(
        self,
        *,
        harness: str | None = None,
        gateway: str | None = None,
        claimed: list[str | None | type[RateLimited]] | None = None,
    ) -> None:
        self.harness = harness
        self.gateway = gateway
        self.claimed = list(claimed or [])
        self.calls: list[tuple[str, str, bool]] = []

    async def harness_role(self, harness_arn: str) -> str | None:
        self.calls.append(("harness", harness_arn, False))
        return self.harness

    async def gateway_role(self, gateway_arn: str) -> str | None:
        self.calls.append(("gateway", gateway_arn, False))
        return self.gateway

    async def claimed_harness_role(self, harness_arn: str, *, fresh: bool = False) -> str | None:
        self.calls.append(("claimed", harness_arn, fresh))
        step = self.claimed.pop(0)
        if step is RateLimited:
            raise RateLimited
        assert not isinstance(step, type)
        return step


def logged(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [json.loads(r.message) for r in caplog.records if r.name == LOGGER]


async def test_harness_hook_identity() -> None:
    identity = await harness_identity(FakeRoles(harness=ROLE), HARNESS_ARN)
    assert identity == AWSIdentityDetails(agent_arn=HARNESS_ARN, principal_arn=ROLE)


async def test_harness_hook_identity_survives_a_failed_lookup() -> None:
    identity = await harness_identity(FakeRoles(harness=None), HARNESS_ARN)
    assert identity == AWSIdentityDetails(agent_arn=HARNESS_ARN)


async def test_iam_call_keeps_the_principal_and_the_gateway() -> None:
    roles = FakeRoles()
    identity = await gateway_identity(
        roles, gateway_arn=GATEWAY_ARN, principal=ROLE, baggage=Baggage()
    )
    assert identity == AWSIdentityDetails(principal_arn=ROLE, gateway_arn=GATEWAY_ARN)
    assert roles.calls == []


async def test_jwt_call_gets_the_gateway_role_and_no_agent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    roles = FakeRoles(gateway=OTHER_ROLE)
    identity = await gateway_identity(
        roles, gateway_arn=GATEWAY_ARN, principal=None, baggage=Baggage(harness_id="h-2")
    )
    assert identity == AWSIdentityDetails(principal_arn=OTHER_ROLE, gateway_arn=GATEWAY_ARN)
    assert roles.calls == [("gateway", GATEWAY_ARN, False)]
    assert logged(caplog) == [{"event": "baggage_unverified", "reason": "no_principal"}]


async def test_a_failed_gateway_role_lookup_keeps_the_gateway(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    identity = await gateway_identity(
        FakeRoles(gateway=None), gateway_arn=GATEWAY_ARN, principal=None, baggage=Baggage()
    )
    assert identity == AWSIdentityDetails(gateway_arn=GATEWAY_ARN)
    assert logged(caplog) == [{"event": "gateway_role_unavailable"}]


async def test_a_verified_harness_id_rebuilds_the_agent_arn() -> None:
    roles = FakeRoles(claimed=[ROLE])
    identity = await gateway_identity(
        roles, gateway_arn=GATEWAY_ARN, principal=ROLE, baggage=Baggage(harness_id="h-2")
    )
    assert identity == AWSIdentityDetails(
        agent_arn=CLAIMED_ARN, principal_arn=ROLE, gateway_arn=GATEWAY_ARN
    )
    assert roles.calls == [("claimed", CLAIMED_ARN, False)]


async def test_a_mismatch_is_re_read_once_then_dropped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    roles = FakeRoles(claimed=[OTHER_ROLE, OTHER_ROLE])
    identity = await gateway_identity(
        roles, gateway_arn=GATEWAY_ARN, principal=ROLE, baggage=Baggage(harness_id="h-2")
    )
    assert identity == AWSIdentityDetails(principal_arn=ROLE, gateway_arn=GATEWAY_ARN)
    assert roles.calls == [("claimed", CLAIMED_ARN, False), ("claimed", CLAIMED_ARN, True)]
    assert logged(caplog) == [{"event": "baggage_unverified", "reason": "mismatch"}]


async def test_a_stale_mismatch_that_the_re_read_fixes_verifies() -> None:
    roles = FakeRoles(claimed=[OTHER_ROLE, ROLE])
    identity = await gateway_identity(
        roles, gateway_arn=GATEWAY_ARN, principal=ROLE, baggage=Baggage(harness_id="h-2")
    )
    assert identity is not None and identity.agent_arn == CLAIMED_ARN


async def test_a_failed_claimed_lookup_is_unverified(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    identity = await gateway_identity(
        FakeRoles(claimed=[None]),
        gateway_arn=GATEWAY_ARN,
        principal=ROLE,
        baggage=Baggage(harness_id="h-2"),
    )
    assert identity is not None and identity.agent_arn is None
    assert logged(caplog) == [{"event": "baggage_unverified", "reason": "lookup_failed"}]


async def test_the_rate_limit_is_unverified(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    identity = await gateway_identity(
        FakeRoles(claimed=[RateLimited]),
        gateway_arn=GATEWAY_ARN,
        principal=ROLE,
        baggage=Baggage(harness_id="h-2"),
    )
    assert identity is not None and identity.agent_arn is None
    assert logged(caplog) == [{"event": "baggage_unverified", "reason": "rate_limited"}]


@pytest.mark.parametrize("harness_id", ["../x", "x" * 65, "a/b", "a b", "a:b"])
async def test_an_invalid_id_is_not_looked_up(
    harness_id: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    roles = FakeRoles()
    identity = await gateway_identity(
        roles, gateway_arn=GATEWAY_ARN, principal=ROLE, baggage=Baggage(harness_id=harness_id)
    )
    assert identity == AWSIdentityDetails(principal_arn=ROLE, gateway_arn=GATEWAY_ARN)
    assert roles.calls == []
    assert logged(caplog) == [{"event": "baggage_unverified", "reason": "invalid_id"}]


async def test_a_64_character_id_is_valid() -> None:
    roles = FakeRoles(claimed=[ROLE])
    identity = await gateway_identity(
        roles, gateway_arn=GATEWAY_ARN, principal=ROLE, baggage=Baggage(harness_id="x" * 64)
    )
    assert identity is not None and identity.agent_arn is not None


async def test_no_gateway_arn_skips_verification(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    roles = FakeRoles()
    identity = await gateway_identity(
        roles, gateway_arn=None, principal=ROLE, baggage=Baggage(harness_id="h-2")
    )
    assert identity == AWSIdentityDetails(principal_arn=ROLE)
    assert roles.calls == []
    assert logged(caplog) == [{"event": "baggage_unverified", "reason": "no_gateway_arn"}]


async def test_a_baggage_without_a_claim_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    await gateway_identity(
        FakeRoles(gateway=None), gateway_arn=None, principal=ROLE, baggage=Baggage()
    )
    assert logged(caplog) == []


async def test_neither_gateway_arn_nor_principal_is_no_identity() -> None:
    roles = FakeRoles()
    assert (
        await gateway_identity(roles, gateway_arn=None, principal=None, baggage=Baggage()) is None
    )
    assert roles.calls == []
