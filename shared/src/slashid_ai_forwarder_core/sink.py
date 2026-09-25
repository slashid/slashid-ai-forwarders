"""Async HTTP client for the SlashID NHI AI invocations endpoints: the push,
and the preflight that asks about an invocation before it is pushed.

The forwarder only authenticates with the connection's event-streaming
token. Identity creation, role-chain unrolling, and conversation
stitching all happen on the SlashID side — the Lambda is intentionally
a one-trick pony.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import httpx
from pydantic import ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .events import AIInvocationObservedV1, AIPreflightResponse

log = logging.getLogger(__name__)

AI_INVOCATIONS_PATH = "/ip/nhi/events/ai-invocations"
PREFLIGHT_PATH = f"{AI_INVOCATIONS_PATH}/preflight"
# Held back from the budget handed to the server, so a verdict it reaches
# at its own deadline still travels back before ours runs out.
PREFLIGHT_RETURN_MARGIN_S = 0.25
# The server's floor; a budget below it is clamped up anyway.
PREFLIGHT_MIN_BUDGET_S = 0.05

# Endpoint allows 1 MB; leave headroom for the wrapping envelope + transport overhead.
MAX_BATCH_BYTES = 900_000


class TransientPushError(Exception):
    """Retryable upstream failure (5xx, 429, network)."""


class PermanentPushError(Exception):
    """Non-retryable upstream failure (other 4xx)."""


class PreflightError(Exception):
    """Preflight gave no verdict: a transport failure, a non-200, or a body
    that is not ``{"deny_reasons": [str, ...]}``."""


def _classify(resp: httpx.Response) -> Exception:
    code = resp.status_code
    body = resp.text[:500]
    msg = f"HTTP {code} from {resp.request.url}: {body}"
    if code == 429 or code >= 500:
        return TransientPushError(msg)
    return PermanentPushError(msg)


async def _request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: Any | None,
    max_retries: int,
) -> httpx.Response:
    """Issue a request with bounded exponential backoff on transient failures."""
    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_exponential(multiplier=0.25, min=0.25, max=4.0),
        retry=retry_if_exception_type((TransientPushError, httpx.TransportError)),
        reraise=True,
    )
    async for attempt in retrying:
        with attempt:
            resp = await client.request(method, url, headers=headers, json=json_body)
            if resp.status_code >= 400:
                raise _classify(resp)
            return resp
    raise RuntimeError("unreachable: AsyncRetrying always raises or returns")


def _event_wire_bytes(event: AIInvocationObservedV1) -> int:
    """JSON byte size of the wire form for batching arithmetic."""
    return len(event.model_dump_json(exclude_none=True))


def _batch_events(
    events: Iterable[AIInvocationObservedV1],
) -> Iterable[list[AIInvocationObservedV1]]:
    """Split events into batches whose serialized `{events: [...]}` stays under 1 MB."""
    envelope_overhead = len('{"events":[]}')
    batch: list[AIInvocationObservedV1] = []
    size = envelope_overhead
    for ev in events:
        ev_bytes = _event_wire_bytes(ev)
        delta = ev_bytes + (1 if batch else 0)
        if batch and size + delta > MAX_BATCH_BYTES:
            yield batch
            batch = [ev]
            size = envelope_overhead + ev_bytes
        else:
            batch.append(ev)
            size += delta
    if batch:
        yield batch


def _events_payload(events: list[AIInvocationObservedV1]) -> dict[str, list[dict[str, Any]]]:
    return {
        "events": [ev.model_dump(mode="json", exclude_none=True) for ev in events],
    }


def _redact_content(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with redacted_text stripped from input/output."""
    out = dict(event_dict)
    for field in ("input", "output"):
        if isinstance(out.get(field), dict) and "redacted_text" in out[field]:
            out[field] = {k: v for k, v in out[field].items() if k != "redacted_text"}
    return out


async def push_invocations(
    client: httpx.AsyncClient,
    events: list[AIInvocationObservedV1],
    *,
    endpoint: str,
    push_token: str,
    max_retries: int = 3,
) -> int:
    """POST /ip/nhi/events/ai-invocations in 1 MB batches. Returns the count sent."""
    if not events:
        return 0
    url = f"{endpoint}{AI_INVOCATIONS_PATH}"
    headers = {"Authorization": f"Bearer {push_token}"}

    sent = 0
    for batch in _batch_events(events):
        payload = _events_payload(batch)
        await _request_with_retry(
            client,
            "POST",
            url,
            headers=headers,
            json_body=payload,
            max_retries=max_retries,
        )
        sent += len(batch)
        log.info("push_invocations: batch of %d events posted (total=%d)", len(batch), sent)
        if log.isEnabledFor(logging.DEBUG):
            for ev in payload["events"]:
                log.debug("push_invocations: event payload: %s", _redact_content(ev))
    return sent


async def preflight_invocation(
    client: httpx.AsyncClient,
    invocation: AIInvocationObservedV1,
    *,
    endpoint: str,
    push_token: str,
    timeout_s: float,
) -> list[str]:
    """POST /ip/nhi/events/ai-invocations/preflight. Returns the deny reasons.

    The body is the same ``AIInvocationObservedV1`` the push carries, sent
    before the model has answered and therefore incomplete. An empty list
    allows. A server-side check that cannot complete denies with a reason of
    its own, so only a failure to get a verdict at all raises
    ``PreflightError``.

    No retry: the caller is inside a budget of ``timeout_s``. The server gets
    that budget less ``PREFLIGHT_RETURN_MARGIN_S`` through
    ``SlashID-Request-Timeout``, because it spends all of what it is given and
    denies when it runs out; handed the whole budget, that deny would arrive
    just as we stop waiting and become our own fail mode instead.
    """
    server_budget_s = max(timeout_s - PREFLIGHT_RETURN_MARGIN_S, PREFLIGHT_MIN_BUDGET_S)
    try:
        response = await client.post(
            f"{endpoint}{PREFLIGHT_PATH}",
            json=invocation.model_dump(mode="json", exclude_none=True),
            headers={
                "Authorization": f"Bearer {push_token}",
                "SlashID-Request-Timeout": f"{server_budget_s:.3f}",
            },
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise PreflightError(repr(exc)) from exc
    if response.status_code != 200:
        raise PreflightError(f"HTTP {response.status_code}")
    try:
        return AIPreflightResponse.model_validate_json(response.content).deny_reasons
    except ValidationError as exc:
        raise PreflightError("unparseable verdict") from exc
