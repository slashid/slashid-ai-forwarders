import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from botocore.session import get_session as botocore_session
from botocore.validate import validate_parameters

from slashid_agentcore_forwarder.logs import RoleKind, RoleLookupError
from slashid_agentcore_forwarder.roles import (
    CLAIMED_MAX_ENTRIES,
    SHARED_MAX_ENTRIES,
    RateLimited,
    Roles,
    _Control,
    control_config,
)

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


def make_roles(control: FakeControl, clock: Clock | None = None, **limits: int) -> Roles:
    async def provide() -> FakeControl:
        return control

    return Roles(provide, clock=clock or Clock(), lookup_timeout_s=0.05, **limits)


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
    roles = make_roles(control, clock, claimed_max_entries=5)
    await roles.harness_role(HARNESS_ARN)
    for i in range(12):
        await roles.claimed_harness_role(f"arn:aws:bedrock-agentcore:r:1:harness/h{i}")
        clock.now += 7
    before = len(control.calls)
    await roles.harness_role(HARNESS_ARN)
    assert len(control.calls) == before
    # The claimed cache really did overflow: the oldest id is gone, the newest is kept.
    await roles.claimed_harness_role("arn:aws:bedrock-agentcore:r:1:harness/h11")
    assert len(control.calls) == before
    await roles.claimed_harness_role("arn:aws:bedrock-agentcore:r:1:harness/h0")
    assert len(control.calls) == before + 1


async def test_the_claimed_cache_holds_the_most_recent_entries_up_to_its_bound() -> None:
    control = FakeControl()
    roles = make_roles(control, claimed_max_entries=3)
    for i in range(4):
        await roles.claimed_harness_role(f"arn:x:harness/h{i}")
    await roles.claimed_harness_role("arn:x:harness/h3")
    await roles.claimed_harness_role("arn:x:harness/h1")
    assert len(control.calls) == 4
    await roles.claimed_harness_role("arn:x:harness/h0")
    assert len(control.calls) == 5


async def test_a_claimed_entry_expires_at_60_seconds() -> None:
    clock, control = Clock(), FakeControl()
    roles = make_roles(control, clock)
    await roles.claimed_harness_role(HARNESS_ARN)
    clock.now += 59
    await roles.claimed_harness_role(HARNESS_ARN)
    assert len(control.calls) == 1
    clock.now += 2
    await roles.claimed_harness_role(HARNESS_ARN)
    assert len(control.calls) == 2


async def test_the_shared_cache_is_bounded() -> None:
    control = FakeControl()
    roles = make_roles(control)
    for i in range(SHARED_MAX_ENTRIES + 1):
        await roles.harness_role(f"arn:x:harness/h{i}")
    assert len(control.calls) == SHARED_MAX_ENTRIES + 1
    await roles.harness_role(f"arn:x:harness/h{SHARED_MAX_ENTRIES}")
    assert len(control.calls) == SHARED_MAX_ENTRIES + 1
    await roles.harness_role("arn:x:harness/h0")
    assert len(control.calls) == SHARED_MAX_ENTRIES + 2


def test_the_cache_bounds_are_the_specified_ones() -> None:
    assert (SHARED_MAX_ENTRIES, CLAIMED_MAX_ENTRIES) == (1000, 100)


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


def test_the_client_config_makes_one_attempt() -> None:
    options = vars(control_config())  # botocore sets its options as plain attributes
    assert options["retries"] == {"total_max_attempts": 1}
    assert (options["connect_timeout"], options["read_timeout"]) == (0.5, 0.5)


LOGGER = "slashid_agentcore_forwarder"


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "secret-detail"}}, "GetHarness")


def failure_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    rows = [json.loads(r.message) for r in caplog.records if r.name == LOGGER]
    return [row for row in rows if row["event"] == "role_lookup_failed"]


@pytest.mark.parametrize(
    ("raised", "error"),
    [
        (TimeoutError(), "timeout"),
        (client_error("AccessDeniedException"), "access_denied"),
        (client_error("AccessDenied"), "access_denied"),
        (client_error("ResourceNotFoundException"), "not_found"),
        (client_error("ThrottlingException"), "throttled"),
        (client_error("TooManyRequestsException"), "throttled"),
        (client_error("InternalServerException"), "other"),
        (ClientError({}, "GetHarness"), "other"),
        (RuntimeError("secret-detail"), "other"),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, Exception) else str(v),
)
@pytest.mark.parametrize("kind", ["harness", "gateway", "claimed_harness"])
async def test_a_failed_lookup_is_logged_by_class_never_by_text(
    kind: RoleKind, raised: Exception, error: RoleLookupError, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)

    def fail(_: str) -> object:
        raise raised

    roles = make_roles(FakeControl(harness=fail, gateway=fail))
    if kind == "gateway":
        assert await roles.gateway_role(GATEWAY_ARN) is None
    elif kind == "harness":
        assert await roles.harness_role(HARNESS_ARN) is None
    else:
        assert await roles.claimed_harness_role(HARNESS_ARN) is None
    assert failure_lines(caplog) == [{"event": "role_lookup_failed", "kind": kind, "error": error}]
    assert "secret-detail" not in caplog.text


async def test_a_slow_lookup_is_logged_as_a_timeout(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await make_roles(FakeControl(delay_s=1)).harness_role(HARNESS_ARN) is None
    assert failure_lines(caplog)[0]["error"] == "timeout"


@pytest.mark.parametrize(
    "response",
    [lambda _: {"harness": {}}, lambda _: {"harness": {"executionRoleArn": ""}}, lambda _: []],
    ids=["missing field", "empty field", "not an object"],
)
async def test_a_response_of_the_wrong_shape_is_logged_as_bad_shape(
    response: Callable[[str], object], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    assert await make_roles(FakeControl(harness=response)).harness_role(HARNESS_ARN) is None
    assert failure_lines(caplog) == [
        {"event": "role_lookup_failed", "kind": "harness", "error": "bad_shape"}
    ]


async def test_a_missing_control_plane_is_logged_as_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def provide() -> FakeControl:
        raise RuntimeError("secret-detail")

    assert await Roles(provide, clock=Clock(), lookup_timeout_s=0.05).gateway_role("g/1") is None
    assert failure_lines(caplog)[0]["error"] == "other"
    assert "secret-detail" not in caplog.text


async def test_the_failure_is_logged_once_not_for_cache_hits_or_refusals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock = Clock()
    roles = make_roles(FakeControl(harness=boom), clock)
    await roles.harness_role(HARNESS_ARN)
    await roles.harness_role(HARNESS_ARN)
    assert len(failure_lines(caplog)) == 1
    for i in range(10):
        await roles.claimed_harness_role(f"arn:x:harness/h{i}")
    assert len(failure_lines(caplog)) == 11
    await roles.claimed_harness_role("arn:x:harness/h3")
    with pytest.raises(RateLimited):
        await roles.claimed_harness_role("arn:x:harness/h99")
    assert len(failure_lines(caplog)) == 11


async def test_a_successful_lookup_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    await make_roles(FakeControl()).harness_role(HARNESS_ARN)
    assert failure_lines(caplog) == []
