"""Async HTTP client for the SlashID NHI endpoints.

Three calls in service of the forwarder pipeline:

1. `POST /nhi/identities/import` — admin-authed, lands `IdentityImportItem[]`.
2. `GET  /nhi/connections`        — admin-authed; pick the `manual_import` one,
   read its `id` + `push_auth_token`.
3. `POST /nhi/events/ai-invocations` — push-token-authed, batched under
   the endpoint's 1 MB body limit.
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

from .events import AIInvocationObservedV1, IdentityImportItem

log = logging.getLogger(__name__)

# Endpoint allows 1 MB; leave headroom for the wrapping envelope + transport overhead.
MAX_BATCH_BYTES = 900_000


class TransientPushError(Exception):
    """Retryable upstream failure (5xx, timeout, network)."""


class PermanentPushError(Exception):
    """Non-retryable upstream failure (4xx other than 429)."""


def _admin_auth_headers(admin_token: str) -> dict[str, str]:
    """Pick the right admin header.

    The SlashID validator accepts either `SlashID-API-Key: <opaque>` or
    `Authorization: Bearer <jwt>`. Detect JWTs by the `eyJ` prefix and two
    dots; everything else is treated as an opaque API key.
    """
    if admin_token.startswith("eyJ") and admin_token.count(".") == 2:
        return {"Authorization": f"Bearer {admin_token}"}
    return {"SlashID-API-Key": admin_token}


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


async def import_identities(
    client: httpx.AsyncClient,
    items: list[IdentityImportItem],
    *,
    endpoint: str,
    admin_token: str,
    org_id: str,
    max_retries: int = 3,
) -> int:
    """POST /nhi/identities/import. Returns the count sent."""
    if not items:
        return 0
    url = f"{endpoint}/nhi/identities/import"
    headers = {**_admin_auth_headers(admin_token), "SlashID-OrgID": org_id}
    body = [i.model_dump(mode="json", exclude_none=True) for i in items]
    await _request_with_retry(
        client, "POST", url, headers=headers, json_body=body, max_retries=max_retries
    )
    log.info("import_identities: %d items posted", len(items))
    return len(items)


async def discover_manual_import_connection(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    admin_token: str,
    org_id: str,
    max_retries: int = 3,
) -> tuple[str, str]:
    """GET /nhi/connections → (connection_id, push_auth_token) for the manual_import conn.

    Raises `PermanentPushError` if no manual_import connection exists.
    """
    url = f"{endpoint}/nhi/connections"
    headers = {**_admin_auth_headers(admin_token), "SlashID-OrgID": org_id}
    resp = await _request_with_retry(
        client, "GET", url, headers=headers, json_body=None, max_retries=max_retries
    )
    data = resp.json()
    conns = data.get("result") if isinstance(data, dict) else data
    if isinstance(conns, dict):
        conns = [conns]
    if not isinstance(conns, list):
        raise PermanentPushError(f"unexpected /nhi/connections payload: {data!r}")

    for c in conns:
        if isinstance(c, dict) and c.get("source") == "manual_import":
            cid = c.get("id")
            tok = c.get("push_auth_token")
            if not cid or not tok:
                raise PermanentPushError(f"manual_import connection {cid} missing push_auth_token")
            log.info("discover_manual_import_connection: %s", cid)
            return str(cid), str(tok)

    raise PermanentPushError("no manual_import connection found; identity import must run first")


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
        await _request_with_retry(
            client,
            "POST",
            url,
            headers=headers,
            json_body=_events_payload(batch),
            max_retries=max_retries,
        )
        sent += len(batch)
        log.info("push_invocations: batch of %d events posted (total=%d)", len(batch), sent)
    return sent
