"""The container-level bundle the adapters share: config, key, checker, roles, input cache."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from slashid_agentcore_forwarder.cache import TtlCache
from slashid_agentcore_forwarder.checks import Checker, Outcome
from slashid_agentcore_forwarder.config import Config, load_config
from slashid_agentcore_forwarder.deny import derive_key
from slashid_agentcore_forwarder.identity import RoleLookup
from slashid_agentcore_forwarder.roles import Roles, default_control
from slashid_agentcore_forwarder.tool_call import CachedInput

INPUT_TTL_S = 600.0
INPUT_MAX_ENTRIES = 1000


class CheckRunner(Protocol):
    async def check(self, invocation: AIInvocationObservedV1, *, started: float) -> Outcome: ...


@dataclass(frozen=True)
class Runtime:
    config: Config
    key: bytes
    checker: CheckRunner
    roles: RoleLookup
    inputs: TtlCache[CachedInput]


def build_runtime(config: Config) -> Runtime:
    return Runtime(
        config=config,
        key=derive_key(config.push_token),
        checker=Checker(config),
        roles=Roles(default_control),
        inputs=TtlCache[CachedInput](ttl_s=INPUT_TTL_S, max_entries=INPUT_MAX_ENTRIES),
    )


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """Built on first use from the environment, then kept for the life of the container."""
    global _runtime
    if _runtime is None:
        _runtime = build_runtime(load_config())
    return _runtime


def reset_runtime() -> None:
    """For tests."""
    global _runtime
    _runtime = None
