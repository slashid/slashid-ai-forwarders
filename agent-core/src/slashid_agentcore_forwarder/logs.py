"""The structured log lines: one JSON object per line, known keys only, never a payload."""

from __future__ import annotations

import json
import logging

log = logging.getLogger("slashid_agentcore_forwarder")


def _emit(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    present = {key: value for key, value in fields.items() if value is not None}
    log.log(level, json.dumps({"event": event, **present}))


def fail_mode(*, surface: str, check: str, reason: str, mode: str) -> None:
    """A check gave no answer and the fail mode decided."""
    _emit("fail_mode", surface=surface, check=check, reason=reason, mode=mode)


def pass_through(*, surface: str, reason: str, keys: list[str] | None = None) -> None:
    """A payload we do not handle went through untouched."""
    _emit("pass_through", surface=surface, reason=reason, keys=keys)


def shadow(*, surface: str, check: str, verdict: str, answered: str) -> None:
    """Shadow mode: the verdict's action and what was answered instead."""
    _emit("shadow", surface=surface, check=check, verdict=verdict, answered=answered)


def truncated_input(*, surface: str, tool: str, call_id: str) -> None:
    _emit("truncated_input", surface=surface, tool=tool, call_id=call_id)


def baggage_unverified(reason: str) -> None:
    _emit("baggage_unverified", reason=reason)


def gateway_role_unavailable() -> None:
    _emit("gateway_role_unavailable")


def cache_miss(*, tool: str, call_id: str, known: bool) -> None:
    """A result with no cached call input; info when the call was seen, else debug."""
    _emit(
        "cache_miss",
        level=logging.INFO if known else logging.DEBUG,
        tool=tool,
        call_id=call_id,
        known=known,
    )


def unreadable(*, surface: str, check: str, reason: str) -> None:
    _emit("unreadable", surface=surface, check=check, reason=reason)


def missing_request_id() -> None:
    _emit("missing_request_id")
