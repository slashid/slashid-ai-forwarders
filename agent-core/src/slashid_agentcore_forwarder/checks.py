"""The check runner: one budget, one retry, and the fail and shadow modes."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.sink import (
    PREFLIGHT_MIN_BUDGET_S,
    PreflightError,
    preflight_invocation,
)

from slashid_agentcore_forwarder import logs
from slashid_agentcore_forwarder.config import Config
from slashid_agentcore_forwarder.deny import deny_text, with_reference

POLICY_UNAVAILABLE = "Your organization's policy check is unavailable."
UNREADABLE = "Your organization's policy check could not read this call."
# A connection idle longer than this may have died in a Lambda freeze.
IDLE_REPLACE_S = 4.0


@dataclass(frozen=True)
class Outcome:
    kind: Literal["allow", "deny", "no_answer"]
    reasons: list[str] = field(default_factory=list)
    why: logs.FailReason = "error"  # a no-answer's reason


@dataclass(frozen=True)
class Decision:
    deny: str | None  # the text to apply; None answers allow


class Checker:
    """One shared client: replaced when idle past a Lambda freeze, one retry after a transport
    error."""

    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        make_client: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._clock = clock
        self._monotonic = monotonic
        self._make_client = make_client
        self._client: httpx.AsyncClient | None = None
        self._last_used = 0.0

    async def _replace(self) -> None:
        old, self._client = self._client, None
        if old is not None:
            with contextlib.suppress(Exception):  # being discarded anyway
                await old.aclose()

    async def _current(self) -> httpx.AsyncClient:
        if self._client is not None and self._clock() - self._last_used > IDLE_REPLACE_S:
            await self._replace()
        if self._client is None:
            self._client = self._make_client()
        return self._client

    async def check(self, invocation: AIInvocationObservedV1, *, started: float) -> Outcome:
        """``started`` is ``monotonic()`` when the budget began (after any role lookup)."""
        retried = False
        while True:
            remaining = self._config.verdict_budget_ms / 1000 - (self._monotonic() - started)
            if remaining < PREFLIGHT_MIN_BUDGET_S:
                return Outcome("no_answer", why="budget")
            client = await self._current()
            try:
                # httpx's timeout is per phase; this bounds a slow drip as well.
                async with asyncio.timeout(remaining):
                    reasons = await preflight_invocation(
                        client,
                        invocation,
                        endpoint=self._config.endpoint,
                        push_token=self._config.push_token,
                        timeout_s=remaining,
                    )
            except PreflightError as exc:
                if exc.cause == "transport" and not retried:
                    retried = True
                    await self._replace()
                    continue
                if exc.cause in ("status", "unparseable"):
                    self._last_used = self._clock()
                return Outcome("no_answer", why=exc.cause)
            except TimeoutError:
                return Outcome("no_answer", why="timeout")
            except Exception:
                return Outcome("no_answer", why="error")
            self._last_used = self._clock()
            return Outcome("deny", reasons) if reasons else Outcome("allow")


def _apply(
    deny: str,
    *,
    config: Config,
    surface: str,
    check: str,
    verdict: str,
) -> Decision:
    """The deny, unless shadow mode answers allow in its place."""
    if config.shadow_mode:
        logs.shadow(surface=surface, check=check, verdict=verdict, answered="allow")
        return Decision(None)
    return Decision(deny)


def decide(
    outcome: Outcome, *, config: Config, key: bytes, surface: str, check: str, prefix: str = ""
) -> Decision:
    """``prefix`` leads a deny's text: ``Content withheld: `` on a result check."""
    if outcome.kind == "allow":
        return Decision(None)
    if outcome.kind == "deny":
        text = deny_text(outcome.reasons, key, prefix=prefix)
        return _apply(text, config=config, surface=surface, check=check, verdict="deny")
    mode = config.verdict_fail_mode
    logs.fail_mode(surface=surface, check=check, reason=outcome.why, mode=mode)
    if mode == "allow":
        return Decision(None)
    text = with_reference(prefix + POLICY_UNAVAILABLE, key)
    return _apply(text, config=config, surface=surface, check=check, verdict="deny")


def unreadable(
    *,
    config: Config,
    key: bytes,
    surface: str,
    check: str,
    reason: logs.UnreadableReason,
    prefix: str = "",
) -> Decision:
    """A call we cannot read is denied whatever the fail mode: that mode is for SlashID being
    unavailable, not for a caller choosing what we can read."""
    logs.unreadable(surface=surface, check=check, reason=reason)
    logs.fail_mode(surface=surface, check=check, reason="unreadable", mode="deny")
    text = with_reference(prefix + UNREADABLE, key)
    return _apply(text, config=config, surface=surface, check=check, verdict="deny")
