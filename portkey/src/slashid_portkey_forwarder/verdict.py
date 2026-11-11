"""The answer to one webhook call.

SlashID's check either answers (an empty list of deny reasons allows) or
does not: a timeout, a transport failure, a non-200. Only then does the
fail mode decide, as it does for a request that cannot be judged at all.
Shadow mode evaluates and logs, then answers allow; the ``Decision`` keeps
both so the log line can say what would have happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.sink import PreflightError, join_deny_reasons, preflight_verdict

from .config import Config

log = logging.getLogger(__name__)

UNAVAILABLE = "Your organization's policy check is unavailable."


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    deny_reasons: tuple[str, ...] = ()
    # Which path decided: "preflight", "fail_mode", "shadow", "none".
    source: str = "none"
    # The rule that denied and the policy it belongs to, when SlashID names
    # them. A deny no rule decided (the policy's default) names the policy.
    rule_id: str | None = None
    policy_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """What Portkey's webhook guardrail reads: ``verdict``, and ``data``,
        which it copies into the ``hook_results`` the caller receives."""
        if self.allowed:
            return {"verdict": True}
        named = {"rule_id": self.rule_id, "policy_id": self.policy_id}
        return {
            "verdict": False,
            "data": {
                "deny_reasons": list(self.deny_reasons),
                "explanation": join_deny_reasons(self.deny_reasons),
                **{key: value for key, value in named.items() if value},
            },
        }


ALLOW = Verdict(True)


@dataclass(frozen=True)
class Decision:
    """What was decided, and what was answered. They differ only under shadow mode."""

    composed: Verdict
    answered: Verdict


def _answer(config: Config, composed: Verdict) -> Decision:
    if config.shadow_mode and not composed.allowed:
        return Decision(composed=composed, answered=Verdict(True, source="shadow"))
    return Decision(composed=composed, answered=composed)


def unjudged(config: Config, why: str) -> Decision:
    """The fail mode, for a request SlashID gave no verdict on."""
    log.warning("verdict: %s; applying fail mode %s", why, config.verdict_fail_mode)
    if config.verdict_fail_mode == "deny":
        return _answer(config, Verdict(False, (UNAVAILABLE,), source="fail_mode"))
    return _answer(config, Verdict(True, source="fail_mode"))


async def decide(
    event: AIInvocationObservedV1, *, token: str, config: Config, client: httpx.AsyncClient
) -> Decision:
    """Ask SlashID about ``event`` with the caller's own token."""
    try:
        answer = await preflight_verdict(
            client,
            event,
            endpoint=config.endpoint,
            push_token=token,
            timeout_s=config.verdict_budget_ms / 1000,
            enforcement="shadow" if config.shadow_mode else "enforce",
        )
    except PreflightError as exc:
        return unjudged(config, f"preflight gave no verdict ({exc.cause}): {exc}")
    reasons = answer.deny_reasons
    composed = Verdict(
        not reasons,
        tuple(reasons),
        source="preflight",
        rule_id=answer.decision.rule_id if answer.decision else None,
        policy_id=answer.decision.policy_id if answer.decision else None,
    )
    decision = _answer(config, composed)
    log.info(
        "verdict %s: %s, answered %s (shadow_mode=%s, rule=%s, policy=%s)",
        event.request_id,
        "allow" if composed.allowed else "deny",
        "allow" if decision.answered.allowed else "deny",
        config.shadow_mode,
        composed.rule_id,
        composed.policy_id,
    )
    return decision
