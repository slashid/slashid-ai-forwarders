"""Preflight and push to SlashID, or, with ``dry_run``, log what would be sent."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.sink import (
    PREFLIGHT_MIN_BUDGET_S,
    PreflightError,
    preflight_invocation,
    push_invocations,
)

from .config import CodexConfig

log = logging.getLogger(__name__)

DRY_RUN_DELAY_S = 1.0


def _connection_lost(exc: PreflightError) -> bool:
    """A connection-level error (reset, refused, DNS), such as a kept-alive
    connection that died while the machine slept; not a timeout or an HTTP
    error."""
    cause = exc.__cause__
    return isinstance(cause, httpx.NetworkError | httpx.RemoteProtocolError) and not isinstance(
        cause, httpx.TimeoutException
    )


class CodexSink:
    def __init__(self, config: CodexConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client

    async def preflight(self, invocation: AIInvocationObservedV1, *, deadline: float) -> list[str]:
        """Deny reasons for ``invocation``; ``deadline`` is a ``time.monotonic()``
        instant. Raises ``PreflightError`` when no verdict arrives by then."""
        if self._config.dry_run:
            await asyncio.sleep(DRY_RUN_DELAY_S)
            log.info("dry run preflight: %s", invocation.model_dump_json(exclude_none=True))
            return []
        retried = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining < PREFLIGHT_MIN_BUDGET_S:
                raise PreflightError("deadline exceeded")
            try:
                return await preflight_invocation(
                    self._client,
                    invocation,
                    endpoint=self._config.endpoint,
                    push_token=self._config.push_token,
                    timeout_s=remaining,
                )
            except PreflightError as exc:
                if retried or not _connection_lost(exc):
                    raise
                retried = True
                log.info("preflight connection lost, retrying: %s", exc)

    async def push(self, events: list[AIInvocationObservedV1]) -> int:
        if self._config.dry_run:
            await asyncio.sleep(DRY_RUN_DELAY_S)
            for event in events:
                log.info("dry run push: %s", event.model_dump_json(exclude_none=True))
            return len(events)
        return await push_invocations(
            self._client,
            events,
            endpoint=self._config.endpoint,
            push_token=self._config.push_token,
            max_retries=self._config.max_retries,
        )
