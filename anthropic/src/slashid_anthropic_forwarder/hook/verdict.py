"""Compose the verdict: two optional checks, concurrently, under one budget.

Owns rule 2 of the design: what to answer when a check cannot. Any deny
denies; a check that gave us no answer takes ``verdict_fail_mode``; a
disabled check is simply absent. Shadow mode evaluates and logs, then
answers allow, and the ``Decision`` keeps both verdicts so the record
stores the one that actually went out.

"Gave us no answer" means a transport failure, a non-200 or a body we
could not parse — never a 200. Preflight fails closed: a server-side check
that cannot complete denies with a reason of its own. So its empty
``deny_reasons`` is a real allow, and running it through the fail mode
would be a bug.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Mapping

import httpx
from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from ..config import Config
from .checks import ALLOW, CheckFailed, Decision, Verdict
from .frame import Frame
from .preflight import preflight_check

log = logging.getLogger(__name__)

# Sticky denials: the offending content stays in the fresh round, so the
# conversation cannot recover. Anthropic's guidance is to say what to
# change; the only true answer is to start over.
RECOVERY = " Start a new conversation to continue."

# Anthropic's cap on the one ``deny_reason`` it accepts.
MAX_DENY_REASON = 500


def _with_recovery(reason: str | None) -> str:
    """The answered reason: a base bounded to leave room, then the sentence.

    The base may be several of preflight's reasons joined by
    ``join_deny_reasons``, since Anthropic takes one string, not a list.
    It is truncated BEFORE the sentence is appended, never after: cutting the joined
    string is what silently deletes the one instruction the person can
    act on, which is the entire point of appending it.
    """
    base = reason or "Blocked by your organization's policy."
    return base[: MAX_DENY_REASON - len(RECOVERY)] + RECOVERY


def reference_id(webhook_id: str) -> str:
    """A stable reference for one delivery: the same value on every retry."""
    return hashlib.sha256(webhook_id.encode()).hexdigest()[:32]


def _fail_mode(config: Config, why: str) -> Verdict:
    log.warning("verdict: %s; applying fail mode %s", why, config.verdict_fail_mode)
    if config.verdict_fail_mode == "deny":
        return Verdict(
            "deny",
            deny_reason="Your organization's policy check is unavailable.",
            source="fail_mode",
        )
    return Verdict("allow", source="fail_mode")


async def _settle(name: str, task: Awaitable[Verdict], config: Config) -> Verdict:
    # ``CheckFailed`` is the only way a check declines to answer; a 200 is
    # always a verdict, including preflight's empty list.
    try:
        return await task
    except CheckFailed as exc:
        return _fail_mode(config, f"{name} failed: {exc}")


def _answer(config: Config, composed: Verdict) -> Decision:
    if config.shadow_mode and composed.denied:
        return Decision(composed=composed, answered=Verdict("allow", source="shadow"))
    return Decision(composed=composed, answered=composed)


def _denied_hash(tail_event: AIInvocationObservedV1 | None, config: Config) -> str | None:
    """The name of the first file carrying a configured digest, or None.

    Judges the same tail event preflight judges, so a test denial and a
    real one are attributed to the same invocation. Pure local work over
    content the frame already carried: it cannot fail, so unlike the two
    remote checks it never interacts with the fail mode.
    """
    denied = config.denied_hashes
    if not denied or tail_event is None or not tail_event.accessed_files:
        return None
    for entry in tail_event.accessed_files:
        for value in (entry.content_hashes or {}).values():
            if value.lower() in denied:
                return entry.name or "A file in this request"
    return None


async def decide(
    frame: Frame,
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    tail_event: AIInvocationObservedV1 | None,
    config: Config,
    client: httpx.AsyncClient,
) -> Decision:
    """``tail_event`` is the invocation being judged: the fresh round of a
    prompt frame, or the requested tool calls of a tool call frame."""
    lower = {k.lower(): v for k, v in headers.items()}
    webhook_id = lower.get("webhook-id", frame.request_id)
    ref = reference_id(webhook_id)

    if frame.type not in ("prompt", "tool_call") or frame.is_connection_test():
        # Neither carries an invocation to judge; the protocol wants allow.
        return _answer(config, Verdict("allow", source="bypass"))

    budget_s = config.verdict_budget_ms / 1000
    checks: list[tuple[str, Awaitable[Verdict]]] = []
    if config.preflight_enabled and tail_event is not None:
        # The tail event is the FRESH round's partial record — the round
        # being judged. The record for the previous assistant run is a
        # different invocation and is never sent here.
        #
        # Every round goes, files or not: the connection's AI policy judges
        # the model and the tools too. No tail event means the builder
        # produced none — what a null actor id does, since the server
        # rejects an identity with no identifier — so there is nothing to
        # send.
        checks.append(
            (
                "preflight",
                preflight_check(
                    client,
                    endpoint=config.endpoint,
                    push_token=config.push_token,
                    invocation=tail_event,
                    timeout_s=budget_s,
                ),
            )
        )

    results: list[Verdict] = []
    if (hit := _denied_hash(tail_event, config)) is not None:
        results.append(
            Verdict(
                "deny",
                deny_reason=f"{hit} is marked as not shareable with AI.",
                source="mock-hash",
            )
        )
    if config.capture_deny_marker and config.capture_deny_marker.encode() in raw_body:
        results.append(
            Verdict(
                "deny",
                deny_reason="Denied by the SlashID capture test marker.",
                source="marker",
            )
        )
    if checks:
        try:
            settled = await asyncio.wait_for(
                asyncio.gather(*(_settle(n, t, config) for n, t in checks)),
                timeout=budget_s,
            )
            results.extend(settled)
        except TimeoutError:
            results.append(_fail_mode(config, "verdict budget exceeded"))

    composed = next((r for r in results if r.denied), ALLOW)
    if composed.denied:
        composed = Verdict(
            "deny",
            deny_reason=_with_recovery(composed.deny_reason),
            reference_id=ref,
            source=composed.source,
        )
    decision = _answer(config, composed)
    log.info(
        "verdict %s: composed %s via %s, answered %s (shadow_mode=%s, checks=%s)",
        webhook_id,
        composed.action,
        composed.source,
        decision.answered.action,
        config.shadow_mode,
        [n for n, _ in checks],
    )
    return decision
