"""Async HTTP client for the SlashID NHI AI invocations endpoint.

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
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .events import AIInvocationObservedV1

log = logging.getLogger(__name__)

# Endpoint allows 1 MB; leave headroom for the wrapping envelope + transport overhead.
MAX_BATCH_BYTES = 900_000


class TransientPushError(Exception):
    """Retryable upstream failure (5xx, 429, network)."""


class PermanentPushError(Exception):
    """Non-retryable upstream failure (other 4xx)."""


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
    """POST /nhi/events/ai-invocations in 1 MB batches. Returns the count sent."""
    if not events:
        return 0
    url = f"{endpoint}/nhi/events/ai-invocations"
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
