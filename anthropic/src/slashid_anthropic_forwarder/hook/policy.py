"""Client for the Go policy receiver (``POST /ai-access/<id>``).

It verifies Anthropic's signature itself, so the frame is forwarded as the
raw bytes received, with the three ``webhook-*`` headers copied verbatim
and nothing else. Re-serializing or compressing would break its check.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from .checks import CheckFailed, Verdict

_FORWARDED = ("webhook-id", "webhook-timestamp", "webhook-signature")


async def policy_check(
    client: httpx.AsyncClient,
    *,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout_s: float,
) -> Verdict:
    lower = {k.lower(): v for k, v in headers.items()}
    forward = {k: lower[k] for k in _FORWARDED if k in lower}
    forward["content-type"] = "application/json"
    try:
        response = await client.post(url, content=body, headers=forward, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise CheckFailed(f"policy: {exc!r}") from exc
    if response.status_code != 200:
        raise CheckFailed(f"policy: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CheckFailed("policy: unparseable verdict") from exc
    action = data.get("action") if isinstance(data, dict) else None
    if action not in ("allow", "deny"):
        raise CheckFailed(f"policy: unknown action {action!r}")
    return Verdict(
        action=action,
        deny_reason=data.get("deny_reason") or None,
        reference_id=data.get("reference_id") or None,
        source="policy",
    )
