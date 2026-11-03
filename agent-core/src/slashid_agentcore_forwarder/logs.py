"""The structured log lines: one JSON object per line, known keys only, never a payload."""

from __future__ import annotations

import json
import logging
from typing import Literal

log = logging.getLogger("slashid_agentcore_forwarder")

# The reasons are fixed vocabularies, so no caller-set text reaches a log line.
FailReason = Literal[
    "transport",
    "timeout",
    "status",
    "unparseable",
    "budget",
    "no_identity",
    "no_request",
    "error",
    "unknown_payload",
    "unknown_version",
    "unknown_event",
    "unreadable",
]
PassThroughReason = Literal["unknown_payload", "surface_disabled", "streaming_response"]
BaggageReason = Literal[
    "no_principal", "no_gateway_arn", "invalid_id", "lookup_failed", "mismatch", "rate_limited"
]
UnreadableReason = Literal["bad_envelope", "bad_tool_call", "batch_with_call"]

MAX_KEYS = 20
MAX_KEY = 64
MAX_NAME = 128


def _emit(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    present = {key: value for key, value in fields.items() if value is not None}
    log.log(level, json.dumps({"event": event, **present}))


def fail_mode(*, surface: str, check: str, reason: FailReason, mode: str) -> None:
    """A check gave no answer and the fail mode decided."""
    _emit("fail_mode", surface=surface, check=check, reason=reason, mode=mode)


def pass_through(*, surface: str, reason: PassThroughReason, keys: list[str] | None = None) -> None:
    """A payload we do not handle went through untouched."""
    bounded = None if keys is None else [key[:MAX_KEY] for key in keys[:MAX_KEYS]]
    _emit("pass_through", surface=surface, reason=reason, keys=bounded)


def shadow(*, surface: str, check: str, verdict: str, answered: str) -> None:
    """Shadow mode: the verdict's action and what was answered instead."""
    _emit("shadow", surface=surface, check=check, verdict=verdict, answered=answered)


def truncated_input(*, surface: str, tool: str, call_id: str) -> None:
    _emit("truncated_input", surface=surface, tool=tool[:MAX_NAME], call_id=call_id[:MAX_NAME])


def baggage_unverified(reason: BaggageReason) -> None:
    _emit("baggage_unverified", reason=reason)


def gateway_role_unavailable() -> None:
    _emit("gateway_role_unavailable")


def cache_miss(*, tool: str, call_id: str, known: bool) -> None:
    """A result with no cached call input; info when the call was seen, else debug."""
    _emit(
        "cache_miss",
        level=logging.INFO if known else logging.DEBUG,
        tool=tool[:MAX_NAME],
        call_id=call_id[:MAX_NAME],
        known=known,
    )


def unreadable(*, surface: str, check: str, reason: UnreadableReason) -> None:
    _emit("unreadable", surface=surface, check=check, reason=reason)


def missing_request_id() -> None:
    _emit("missing_request_id")
