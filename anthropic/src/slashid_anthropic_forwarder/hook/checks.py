"""Types shared by the two verdict checks and their composer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class CheckFailed(Exception):
    """The check could not answer: transport failure, non-200, malformed body.
    The composer applies the configured fail mode."""


@dataclass(frozen=True)
class Verdict:
    action: Literal["allow", "deny"]
    deny_reason: str | None = None
    reference_id: str | None = None
    # Which check decided: "policy", "preflight", "marker", "fail_mode",
    # "bypass", "shadow" (shadow mode answered for it), "none".
    source: str = "none"

    @property
    def denied(self) -> bool:
        return self.action == "deny"

    def to_wire(self) -> dict[str, str]:
        wire: dict[str, str] = {"action": self.action}
        if self.denied:
            if self.deny_reason:
                wire["deny_reason"] = self.deny_reason[:500]
            if self.reference_id:
                wire["reference_id"] = self.reference_id
        return wire


ALLOW = Verdict("allow")


@dataclass(frozen=True)
class Decision:
    """What the checks composed, and what the receiver actually answered.

    They differ only under shadow mode. The pending record stores
    ``answered``: a flush can run an hour later, across a redeploy or a
    mixed-revision rollout, so re-reading shadow mode then would describe a
    configuration that never applied to this call.
    """

    composed: Verdict
    answered: Verdict

    @property
    def blocked(self) -> bool:
        """True only when a deny actually went back to Anthropic."""
        return self.answered.denied
