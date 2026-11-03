import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from botocore.session import get_session as botocore_session
from botocore.validate import validate_parameters

from slashid_agentcore_forwarder.roles import RateLimited, Roles, _Control

FIXTURES = Path(__file__).parent / "fixtures"
HARNESS_ARN = "arn:aws:bedrock-agentcore:us-east-2:123456789012:harness/demo_harness-AbCdEfGhIj"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-2:123456789012:gateway/demo-gateway-abcdefghij"
HARNESS_ROLE = "arn:aws:iam::123456789012:role/demo-harness-execution-role"
GATEWAY_ROLE = "arn:aws:iam::123456789012:role/demo-gateway-role"


def fixture(name: str) -> object:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeControl:
    """Records calls; ``harness`` and ``gateway`` give the response or raise."""

    def __init__(
        self,
        harness: Callable[[str], object] = lambda _: fixture("get_harness"),
        gateway: Callable[[str], object] = lambda _: fixture("get_gateway"),
        delay_s: float = 0,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self._harness = harness
        self._gateway = gateway
        self._delay_s = delay_s

    async def get_harness(self, *, harnessId: str) -> object:
        self.calls.append(("harness", harnessId))
        await asyncio.sleep(self._delay_s)
        return self._harness(harnessId)

    async def get_gateway(self, *, gatewayIdentifier: str) -> object:
        self.calls.append(("gateway", gatewayIdentifier))
        await asyncio.sleep(self._delay_s)
        return self._gateway(gatewayIdentifier)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_roles(control: FakeControl, clock: Clock | None = None) -> Roles:
    async def provide() -> FakeControl:
        return control

    return Roles(provide, clock=clock or Clock(), lookup_timeout_s=0.05)


def boom(_: str) -> object:
    raise RuntimeError("AccessDenied")


async def test_a_harness_lookup_returns_the_role_by_the_last_arn_segment() -> None:
    control = FakeControl()
    assert await make_roles(control).harness_role(HARNESS_ARN) == HARNESS_ROLE
    assert control.calls == [("harness", "demo_harness-AbCdEfGhIj")]


async def test_a_gateway_lookup_returns_the_role() -> None:
    control = FakeControl()
    assert await make_roles(control).gateway_role(GATEWAY_ARN) == GATEWAY_ROLE
    assert control.calls == [("gateway", "demo-gateway-abcdefghij")]


async def test_a_second_lookup_within_five_minutes_is_cached() -> None:
    clock, control = Clock(), FakeControl()
    roles = make_roles(control, clock)
    await roles.harness_role(HARNESS_ARN)
    clock.now += 299
    assert await roles.harness_role(HARNESS_ARN) == HARNESS_ROLE
    assert len(control.calls) == 1
    clock.now += 2
    await roles.harness_role(HARNESS_ARN)
    assert len(control.calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        boom,
        lambda _: {"harness": {}},
        lambda _: {"harness": {"executionRoleArn": ""}},
        lambda _: [],
    ],
    ids=["exception", "missing field", "empty field", "not an object"],
)
async def test_a_failed_lookup_is_none_cached_for_30_seconds(
    response: Callable[[str], object],
) -> None:
    clock, control = Clock(), FakeControl(harness=response)
    roles = make_roles(control, clock)
    assert await roles.harness_role(HARNESS_ARN) is None
    clock.now += 29
    assert await roles.harness_role(HARNESS_ARN) is None
    assert len(control.calls) == 1
    clock.now += 2
    await roles.harness_role(HARNESS_ARN)
    assert len(control.calls) == 2


async def test_a_gateway_response_without_a_role_is_none() -> None:
    roles = make_roles(FakeControl(gateway=lambda _: {"gatewayId": "g"}))
    assert await roles.gateway_role(GATEWAY_ARN) is None


async def test_a_slow_lookup_times_out() -> None:
    roles = make_roles(FakeControl(delay_s=1))
    assert await roles.harness_role(HARNESS_ARN) is None


async def test_no_control_plane_is_none() -> None:
    async def provide() -> FakeControl:
        raise RuntimeError("no credentials")

    roles = Roles(provide, clock=Clock(), lookup_timeout_s=0.05)
    assert await roles.harness_role(HARNESS_ARN) is None


async def test_harness_and_gateway_roles_share_one_cache() -> None:
    control = FakeControl()
    roles = make_roles(control)
    await roles.harness_role("shared-key/id")
    assert await roles.gateway_role("shared-key/id") == HARNESS_ROLE
    assert len(control.calls) == 1


async def test_a_claimed_lookup_has_its_own_cache() -> None:
    control = FakeControl()
    roles = make_roles(control)
    await roles.harness_role(HARNESS_ARN)
    assert await roles.claimed_harness_role(HARNESS_ARN) == HARNESS_ROLE
    assert len(control.calls) == 2
    await roles.claimed_harness_role(HARNESS_ARN)
    assert len(control.calls) == 2


async def test_invented_ids_never_evict_the_harness_entries() -> None:
    clock, control = Clock(), FakeControl()
    roles = make_roles(control, clock)
    await roles.harness_role(HARNESS_ARN)
    for i in range(30):
        await roles.claimed_harness_role(f"arn:aws:bedrock-agentcore:r:1:harness/h{i}")
        clock.now += 7
    before = len(control.calls)
    await roles.harness_role(HARNESS_ARN)
    assert len(control.calls) == before


async def test_a_failed_claimed_lookup_is_cached_for_30_seconds() -> None:
    clock, control = Clock(), FakeControl(harness=boom)
    roles = make_roles(control, clock)
    assert await roles.claimed_harness_role(HARNESS_ARN) is None
    clock.now += 29
    assert await roles.claimed_harness_role(HARNESS_ARN) is None
    assert len(control.calls) == 1
    clock.now += 2
    await roles.claimed_harness_role(HARNESS_ARN)
    assert len(control.calls) == 2


async def test_the_11th_uncached_claimed_lookup_in_a_minute_is_refused() -> None:
    clock, control = Clock(), FakeControl()
    roles = make_roles(control, clock)
    for i in range(10):
        assert await roles.claimed_harness_role(f"arn:x:harness/h{i}") == HARNESS_ROLE
    with pytest.raises(RateLimited):
        await roles.claimed_harness_role("arn:x:harness/h10")
    assert len(control.calls) == 10
    # Cached ids are not lookups.
    assert await roles.claimed_harness_role("arn:x:harness/h3") == HARNESS_ROLE
    # The refusal is not cached, and the window slides.
    clock.now += 61
    assert await roles.claimed_harness_role("arn:x:harness/h10") == HARNESS_ROLE


async def test_fresh_bypasses_the_cache_but_counts_against_the_limit() -> None:
    clock, control = Clock(), FakeControl()
    roles = make_roles(control, clock)
    await roles.claimed_harness_role(HARNESS_ARN)
    await roles.claimed_harness_role(HARNESS_ARN, fresh=True)
    assert len(control.calls) == 2
    for i in range(8):
        await roles.claimed_harness_role(f"arn:x:harness/h{i}")
    with pytest.raises(RateLimited):
        await roles.claimed_harness_role(HARNESS_ARN, fresh=True)


async def test_the_control_adapter_passes_parameters_the_service_accepts() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str]]] = []

        async def get_harness(self, **kwargs: str) -> object:
            self.calls.append(("GetHarness", kwargs))
            return {}

        async def get_gateway(self, **kwargs: str) -> object:
            self.calls.append(("GetGateway", kwargs))
            return {}

    client = Client()
    control = _Control(client)
    await control.get_harness(harnessId="h-1")
    await control.get_gateway(gatewayIdentifier="g-1")
    model = botocore_session().get_service_model("bedrock-agentcore-control")
    for operation, kwargs in client.calls:
        validate_parameters(kwargs, model.operation_model(operation).input_shape)
