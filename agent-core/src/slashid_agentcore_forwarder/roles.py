"""The execution role of a Harness and the role of a Gateway, with caches and limits."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Protocol

from aiobotocore.config import AioConfig
from aiobotocore.session import get_session
from botocore.exceptions import ClientError
from pydantic import Field, ValidationError

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.cache import TtlCache
from slashid_agentcore_forwarder.models import Lenient

LOOKUP_TIMEOUT_S = 1.0
ROLE_TTL_S = 300.0
CLAIMED_TTL_S = 60.0
FAILURE_TTL_S = 30.0
CLAIMED_LOOKUPS_PER_MINUTE = 10
SHARED_MAX_ENTRIES = 1000
CLAIMED_MAX_ENTRIES = 100


class ControlPlane(Protocol):
    async def get_harness(self, *, harnessId: str) -> object: ...
    async def get_gateway(self, *, gatewayIdentifier: str) -> object: ...


class RateLimited(Exception):
    """A claimed lookup was refused: the per-minute limit is spent."""


class _Harness(Lenient):
    executionRoleArn: str = Field(min_length=1)


class _HarnessResponse(Lenient):
    harness: _Harness


class _GatewayResponse(Lenient):
    roleArn: str = Field(min_length=1)


class _ErrorBody(Lenient):
    Code: str = ""


class _ErrorResponse(Lenient):
    Error: _ErrorBody


_ERROR_CODES: dict[str, logs.RoleLookupError] = {
    "AccessDeniedException": "access_denied",
    "AccessDenied": "access_denied",
    "ResourceNotFoundException": "not_found",
    "ThrottlingException": "throttled",
    "TooManyRequestsException": "throttled",
}


def _classify(exc: Exception) -> logs.RoleLookupError:
    """A fixed word for a failure: its class or AWS error code, never its text."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ValidationError):
        return "bad_shape"
    if isinstance(exc, ClientError):
        try:
            code = _ErrorResponse.model_validate(exc.response).Error.Code
        except ValidationError:
            return "other"
        return _ERROR_CODES.get(code, "other")
    return "other"


class Roles:
    def __init__(
        self,
        control: Callable[[], Awaitable[ControlPlane]],
        *,
        clock: Callable[[], float] = time.time,
        lookup_timeout_s: float = LOOKUP_TIMEOUT_S,
        shared_max_entries: int = SHARED_MAX_ENTRIES,
        claimed_max_entries: int = CLAIMED_MAX_ENTRIES,
    ) -> None:
        self._control = control
        self._clock = clock
        self._timeout_s = lookup_timeout_s
        self._shared = TtlCache[str | None](
            ttl_s=ROLE_TTL_S, max_entries=shared_max_entries, clock=clock
        )
        self._claimed = TtlCache[str | None](
            ttl_s=CLAIMED_TTL_S, max_entries=claimed_max_entries, clock=clock
        )
        self._claimed_lookups: deque[float] = deque()

    async def _guarded(
        self, kind: logs.RoleKind, call: Callable[[ControlPlane], Awaitable[str]]
    ) -> str | None:
        """The role, or ``None`` for any failure, a missing permission included; the failure is
        logged by kind. The client comes first, outside the ceiling (its first creation can cost
        more than the ceiling); only the call is bounded."""
        try:
            control = await self._control()
            return await asyncio.wait_for(call(control), self._timeout_s)
        except Exception as exc:
            logs.role_lookup_failed(kind, _classify(exc))
            return None

    async def _harness(self, arn: str, kind: logs.RoleKind) -> str | None:
        async def lookup(control: ControlPlane) -> str:
            response = await control.get_harness(harnessId=_last_segment(arn))
            return _HarnessResponse.model_validate(response).harness.executionRoleArn

        return await self._guarded(kind, lookup)

    async def _gateway(self, arn: str) -> str | None:
        async def lookup(control: ControlPlane) -> str:
            response = await control.get_gateway(gatewayIdentifier=_last_segment(arn))
            return _GatewayResponse.model_validate(response).roleArn

        return await self._guarded("gateway", lookup)

    async def _shared_lookup(
        self, arn: str, lookup: Callable[[str], Awaitable[str | None]]
    ) -> str | None:
        if self._shared.has(arn):
            return self._shared.get(arn)
        role = await lookup(arn)
        self._shared.put(arn, role, ttl_s=None if role else FAILURE_TTL_S)
        return role

    async def harness_role(self, harness_arn: str) -> str | None:
        return await self._shared_lookup(harness_arn, lambda arn: self._harness(arn, "harness"))

    async def gateway_role(self, gateway_arn: str) -> str | None:
        return await self._shared_lookup(gateway_arn, self._gateway)

    async def claimed_harness_role(self, harness_arn: str, *, fresh: bool = False) -> str | None:
        """A role for an id the caller chose: own cache, and at most 10 uncached lookups a
        minute (``RateLimited`` beyond). ``fresh`` re-reads past the cache and counts."""
        if not fresh and self._claimed.has(harness_arn):
            return self._claimed.get(harness_arn)
        now = self._clock()
        while self._claimed_lookups and self._claimed_lookups[0] <= now - 60:
            self._claimed_lookups.popleft()
        if len(self._claimed_lookups) >= CLAIMED_LOOKUPS_PER_MINUTE:
            raise RateLimited
        self._claimed_lookups.append(now)
        role = await self._harness(harness_arn, "claimed_harness")
        self._claimed.put(harness_arn, role, ttl_s=None if role else FAILURE_TTL_S)
        return role


def _last_segment(arn: str) -> str:
    return arn.rsplit("/", 1)[-1]


class _Client(Protocol):
    """The calls we make on an aiobotocore ``bedrock-agentcore-control`` client."""

    async def get_harness(self, **kwargs: str) -> object: ...
    async def get_gateway(self, **kwargs: str) -> object: ...


class _Control:
    """A ``ControlPlane`` over an aiobotocore client."""

    def __init__(self, client: _Client) -> None:
        self._client = client

    async def get_harness(self, *, harnessId: str) -> object:
        return await self._client.get_harness(harnessId=harnessId)

    async def get_gateway(self, *, gatewayIdentifier: str) -> object:
        return await self._client.get_gateway(gatewayIdentifier=gatewayIdentifier)


_STACK = AsyncExitStack()
_control: ControlPlane | None = None


def control_config() -> AioConfig:
    """Short timeouts and no retry: a lookup has a 1 s bound. ``total_max_attempts`` counts the
    first try, where ``max_attempts`` counts retries after it."""
    return AioConfig(connect_timeout=0.5, read_timeout=0.5, retries={"total_max_attempts": 1})


async def default_control() -> ControlPlane:
    """The container's client, entered once; its short timeouts keep a lookup inside the 1 s
    bound."""
    global _control
    if _control is None:
        client = await _STACK.enter_async_context(
            get_session().create_client(
                "bedrock-agentcore-control",
                config=control_config(),
            )
        )
        _control = _Control(client)
    return _control
