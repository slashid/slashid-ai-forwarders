"""Client for ``POST /nhi/ai/preflight``.

The body is an ``AIInvocationObservedV1`` — the same object the sink
pushes once the call completes — sent early and therefore incomplete:
``output``, ``tokens`` and everything the model has not produced yet are
absent, and nothing requires them. There is deliberately no
preflight-specific request schema, so nothing has to be kept in step and
the invocation is not built twice. Only ``accessed_files`` is read today.

The answer is ``{"deny_reasons": [...]}``, always serialized, the empty
array included: empty allows, non-empty denies. A server-side check that
could not run contributes no reason and the call is allowed — that
degradation is recorded on the server's own counter and in its logs,
never on the wire, so there is no "this was a fallback" flag to read and
an empty list is a real allow. Only *our* transport failures — non-200,
timeout, unparseable body — raise ``CheckFailed``, which is what the
composer's fail mode covers.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx
from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from .checks import CheckFailed, Verdict

log = logging.getLogger(__name__)


def join_deny_reasons(reasons: Sequence[str]) -> str:
    """The endpoint's list as the single string Anthropic's ``deny_reason``
    takes.

    Joined with a space, because each reason is a standalone statement
    written to be read by a person and the surface rendering
    ``deny_reason`` is not guaranteed to honour a line break. The server
    already deduplicates, so nothing is dropped here but blanks.
    """
    return " ".join(reason.strip() for reason in reasons if reason.strip())


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

    The credential is the connection push token, carried exactly as
    ``sink.push_invocations`` carries it — the same one, not a second.
    """
    # Every file goes, uncapped. Preflight fails closed, so a batch it cannot
    # finish denies rather than slipping through, which is what makes this
    # safe. A cap here would be the bypass: whatever sat past it would never
    # be checked at all.
    try:
        # ``/ip`` is the route's internal name; the public gateway strips it,
        # as it does for the ingest route the sink calls.
        #
        # ``SlashID-Request-Timeout`` hands the server our own budget, so it
        # bounds its work to what we will actually wait for rather than to a
        # fixed per-check deadline that knows nothing about ours. A server
        # that predates the header ignores it.
        response = await client.post(
            f"{endpoint}/nhi/ai/preflight",
            json=invocation.model_dump(mode="json", exclude_none=True),
            headers={
                "Authorization": f"Bearer {push_token}",
                "SlashID-Request-Timeout": f"{timeout_s:.1f}",
            },
            timeout=timeout_s,
        )
    except httpx.HTTPError as exc:
        raise CheckFailed(f"preflight: {exc!r}") from exc
    if response.status_code != 200:
        raise CheckFailed(f"preflight: HTTP {response.status_code}")
    try:
        reasons = response.json()["deny_reasons"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckFailed("preflight: unparseable verdict") from exc
    if not isinstance(reasons, list) or not all(isinstance(r, str) for r in reasons):
        raise CheckFailed("preflight: deny_reasons is not a list of strings")
    if not reasons:
        return Verdict("allow", source="preflight")
    return Verdict("deny", deny_reason=join_deny_reasons(reasons), source="preflight")
