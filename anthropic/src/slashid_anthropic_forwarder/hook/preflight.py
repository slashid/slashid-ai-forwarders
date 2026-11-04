"""Preflight as a verdict check.

The wire call is ``sink.preflight_invocation``, next to the push it
precedes. This module turns its deny reasons into a ``Verdict``: an empty
list is a real allow, and only a failure to get an answer at all raises
``CheckFailed``, which is what the composer's fail mode covers.
"""

from __future__ import annotations

import httpx
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.sink import PreflightError, join_deny_reasons, preflight_invocation

from .checks import CheckFailed, Verdict


async def preflight_check(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    push_token: str,
    invocation: AIInvocationObservedV1,
    timeout_s: float,
) -> Verdict:
    """Judge ``invocation``: the tail event, the partial record for the
    round about to be sent. The record for the previous assistant run is a
    different invocation and must never be sent here.
    """
    try:
        reasons = await preflight_invocation(
            client, invocation, endpoint=endpoint, push_token=push_token, timeout_s=timeout_s
        )
    except PreflightError as exc:
        raise CheckFailed(f"preflight: {exc}") from exc
    if not reasons:
        return Verdict("allow", source="preflight")
    return Verdict("deny", deny_reason=join_deny_reasons(reasons), source="preflight")
