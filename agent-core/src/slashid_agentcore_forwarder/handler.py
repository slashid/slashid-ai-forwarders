"""Lambda entry point: one event loop, the dispatch of spec 3.2 rules 1, 2 and 4, and the switches.

An exception from the configuration escapes as a Lambda error (AWS's ``failureMode`` then
decides); any other exception is answered as a check that gave no answer.
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal

from pydantic import JsonValue, ValidationError

from slashid_agentcore_forwarder import gateway, harness, logs
from slashid_agentcore_forwarder.config import Config
from slashid_agentcore_forwarder.models import Lenient, Raw
from slashid_agentcore_forwarder.payloads import ClientContext, client_context
from slashid_agentcore_forwarder.runtime import Runtime, get_runtime

# The package logger only: at DEBUG the root level would make aiobotocore log the signed request
# (Authorization and session token). Records still reach Lambda's handler on the root logger.
logs.log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

Surface = Literal["harness", "gateway"]
SURFACES: tuple[Surface, ...] = ("harness", "gateway")
VERSION = "1.0"
TOOL_EVENTS = ("before_tool_call", "after_tool_call")
INVOCATION_EVENTS = ("before_invocation", "after_invocation")

_LOOP = asyncio.new_event_loop()
_announced = False


class Discriminators(Lenient):
    """The top-level keys that place a payload; ``model_fields_set`` tells which are present."""

    event: Raw = None
    interceptorInputVersion: Raw = None
    mcp: Raw = None
    http: Raw = None
    gatewayRequest: Raw = None
    context: Raw = None


def reset_announced() -> None:
    """For tests."""
    global _announced
    _announced = False


def _announce_disabled(config: Config) -> None:
    """One ``pass_through`` line per disabled surface, at the container's first invocation."""
    global _announced
    if _announced:
        return
    _announced = True
    for surface in SURFACES:
        if not _enabled(config, surface):
            logs.pass_through(surface=surface, reason="surface_disabled")


def _enabled(config: Config, surface: Surface) -> bool:
    return config.harness_hooks_enabled if surface == "harness" else config.gateway_hooks_enabled


def _surface(found: Discriminators) -> Surface | None:
    """The surface a payload belongs to, by its discriminator, else by the AWS keys it carries
    (an ``interceptorInputVersion`` wins over an ``event``)."""
    present = found.model_fields_set
    if "interceptorInputVersion" in present:
        return "gateway"
    if "event" in present:
        return "harness"
    if present & {"mcp", "http", "gatewayRequest"}:
        return "gateway"
    return "harness" if "context" in present else None


def _peek(event: object) -> Discriminators:
    try:
        return Discriminators.model_validate(event)
    except ValidationError:
        return Discriminators()


def _place(found: Discriminators, surface: Surface) -> logs.FailReason | None:
    """Why a payload cannot be placed (rule 1), else None."""
    present = found.model_fields_set
    if surface == "harness":
        if "event" not in present:
            return "unknown_payload"
        known = found.event in (*TOOL_EVENTS, *INVOCATION_EVENTS)
        return None if known else "unknown_event"
    if "interceptorInputVersion" not in present or "event" in present:
        return "unknown_payload"
    if found.interceptorInputVersion != VERSION:
        return "unknown_version"
    return None if found.mcp is not None or found.http is not None else "unknown_payload"


async def _route(runtime: Runtime, event: dict[str, JsonValue], ctx: ClientContext) -> JsonValue:
    found = Discriminators.model_validate(event)
    surface = _surface(found)
    if surface is None:
        logs.pass_through(surface="unknown", reason="unknown_payload", keys=list(event))
        return harness.allow()
    _announce_disabled(runtime.config)
    http = found.http is not None and found.mcp is None
    if not _enabled(runtime.config, surface):
        return (
            harness.allow() if surface == "harness" else gateway.passthrough(found.mcp, http=http)
        )
    unplaced = _place(found, surface)
    if surface == "harness":
        if unplaced is not None:
            return harness.no_answer(runtime, unplaced, "call")
        if found.event in INVOCATION_EVENTS:
            return harness.allow()
        return await harness.handle(runtime, event)
    if unplaced is not None:
        return gateway.unplaced(runtime, found.mcp, unplaced)
    return await gateway.handle(runtime, event, ctx)


def _failed(runtime: Runtime, event: object, *, unreadable: bool) -> JsonValue:
    """The answer for an exception that reached the entry point. The surface is read again from
    the payload; one that cannot be read gets the Gateway shape."""
    found = _peek(event)
    surface = _surface(found)
    if surface == "harness":
        check: harness.Check = "result" if found.event == "after_tool_call" else "call"
        if unreadable:
            return harness.deny_unreadable(runtime, "too_deep", check)
        return harness.no_answer(runtime, "error", check)
    if unreadable:
        return gateway.deny_unreadable(runtime, found.mcp, "too_deep")
    return gateway.unplaced(runtime, found.mcp, "error")


async def dispatch(runtime: Runtime, event: dict[str, JsonValue], ctx: ClientContext) -> JsonValue:
    """Rule 4: an exception is a check that gave no answer, but a ``RecursionError`` is a payload
    we cannot read, denied whatever the fail mode."""
    try:
        return await _route(runtime, event, ctx)
    except RecursionError:
        return _failed(runtime, event, unreadable=True)
    except Exception as exc:
        surface = _surface(_peek(event)) or "unknown"
        logs.unexpected_error(surface=surface, error=type(exc).__name__)
        return _failed(runtime, event, unreadable=False)


def lambda_handler(event: dict[str, JsonValue], context: object) -> JsonValue:
    return _LOOP.run_until_complete(dispatch(get_runtime(), event, client_context(context)))
