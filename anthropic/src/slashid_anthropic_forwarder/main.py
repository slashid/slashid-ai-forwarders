"""FastAPI entrypoint: the hook on any path, the flush on /tick or a local timer.

Owns rule 1 of the design: nothing after the verdict is decided — the
capture, the pending write, the push — may change the response. A non-200
is a webhook failure, which hands control to the organization's
fail-open/fail-closed setting, and sustained failures trip Anthropic's
circuit breaker and disable enforcement entirely.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from asyncio import sleep
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from slashid_ai_forwarder_core.platform import BlobSink, SchedulerAuth, TickLease

from .compliance.checkpoint import Cursors
from .compliance.readers import run_readers
from .config import Config, load_config
from .hook.capture import capture_frame
from .hook.checks import ALLOW, Decision
from .hook.frame import Frame, PromptFrame, ToolCallFrame
from .hook.signature import verify
from .hook.toolcall import tool_call_event
from .hook.verdict import decide
from .pending import TICK_LEASE, flush_due, unanswered_round, write_from_frame
from .platform import Backends, open_backends
from .store import PendingStore

log = logging.getLogger(__name__)


async def _capture_safely(capture: BlobSink, request_id: str, headers: dict, body: bytes) -> None:
    try:
        await capture_frame(capture, request_id, headers, body)
    except Exception:
        log.exception("capture failed for %s", request_id)


async def _write_safely(**kwargs: Any) -> None:
    """Rule 1's other half: the write runs after the response is sent, and
    its failure is a log line, never a status code."""
    try:
        await write_from_frame(**kwargs)
    except Exception:
        log.exception("pending write failed for %s", kwargs.get("webhook_id"))


def _parse(body: bytes) -> PromptFrame | ToolCallFrame | None:
    try:
        raw = json.loads(body)
        if Frame.model_validate(raw).type == "tool_call":
            return ToolCallFrame.model_validate(raw)
        return PromptFrame.model_validate(raw)
    except Exception:
        return None


def _signed_at(headers: dict[str, str]) -> int:
    """The attested webhook-timestamp. Falling back to now keeps an
    unsigned-mode deployment from losing the event over a missing header."""
    try:
        return int(headers["webhook-timestamp"])
    except (KeyError, ValueError):
        return int(time.time())


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


async def _refuse(_token: str) -> bool:
    """The default when nothing was injected. Fail closed: a tick route
    that authenticates nobody is one anybody can drive."""
    return False


def create_app(
    config: Config,
    *,
    capture: BlobSink | None = None,
    store: PendingStore | None = None,
    lease: TickLease | None = None,
    cursors: Cursors | None = None,
    tick_auth: SchedulerAuth | None = None,
    client: httpx.AsyncClient | None = None,
    backends: Callable[[], contextlib.AbstractAsyncContextManager[Backends]] | None = None,
) -> FastAPI:
    """Every stateful piece is injected, or opened at startup by ``backends``
    (which ``app()`` points at the configured platform) and closed at
    shutdown; tests hand in fakes."""
    authorize = tick_auth or _refuse
    held: dict[str, httpx.AsyncClient | None] = {"client": client}

    def http() -> httpx.AsyncClient:
        # Lazily built and shared: ASGITransport does not run lifespan
        # events, so tests inject their own rather than relying on startup.
        if held["client"] is None:
            held["client"] = httpx.AsyncClient(timeout=config.request_timeout_seconds)
        return held["client"]

    async def run_tick() -> dict[str, Any]:
        if store is None:
            return {"flushed": 0}
        guard = lease.hold(TICK_LEASE) if lease is not None else contextlib.nullcontext(True)
        async with guard as leased:
            if not leased:
                # Not an error: the next tick picks the same work up from the store.
                return {"flushed": 0, "skipped": True}
            counters: dict[str, int] = {}
            # Readers first: a `complete` here can make a record ready,
            # and it should go out on this tick rather than the next.
            try:
                counters = await run_readers(
                    store=store, config=config, http=http(), cursors=cursors
                )
            except Exception:
                # Never a failed tick: the next one re-runs the pass from its watermark.
                log.exception("tick: the reader pass failed; flushing anyway")
            flushed = await flush_due(store, config=config, client=http())
        result = {"flushed": flushed, **counters}
        log.info("tick: %s", result)
        return result

    async def tick_forever() -> None:
        while True:
            await sleep(config.tick_interval_seconds)
            try:
                await run_tick()
            except Exception:
                log.exception("tick failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The routes read these names when a request arrives, so rebinding
        # them here is all the opened backends need.
        nonlocal capture, store, lease, cursors, authorize

        if config.hook_enabled and not config.hook_verifies_signatures:
            log.warning("no SLASHID_HOOK_SIGNING_SECRET: the hook accepts unsigned requests")

        async def close_client() -> None:
            if client is None and held["client"] is not None:
                await held["client"].aclose()

        async with contextlib.AsyncExitStack() as stack:
            if backends is not None:
                opened = await stack.enter_async_context(backends())
                capture, store, lease, cursors = (
                    opened.capture,
                    opened.store,
                    opened.lease,
                    opened.cursors,
                )
                authorize = opened.tick_auth
            # Registered last, so it closes first: before the platform, and
            # even if the platform's exit raises.
            stack.push_async_callback(close_client)
            if config.platform == "local":
                ticker = asyncio.create_task(tick_forever())

                async def stop_ticker() -> None:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)

                stack.push_async_callback(stop_ticker)
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def handle_frame(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        if len(body) > config.max_body_bytes:
            return Response(status_code=413)
        headers = {k: v for k, v in request.headers.items()}
        if config.hook_verifies_signatures:
            if not verify(config.signing_secrets, headers, body):
                return Response(status_code=401)
        elif not config.hook_enabled:
            return Response(status_code=401)
        webhook_id = headers.get("webhook-id", "")

        if capture is not None:
            background.add_task(_capture_safely, capture, webhook_id, headers, body)

        frame = _parse(body)
        if frame is None:
            # The frame is inspected, not validated for the verdict: a
            # shape we cannot parse is answered, not rejected.
            log.warning("frame %s did not parse; allowing", webhook_id)
            return JSONResponse(ALLOW.to_wire(), background=background)
        if isinstance(frame, ToolCallFrame):
            # One verdict for every call in the response, judged before any
            # runs. Not an invocation: it writes no record.
            decision = await decide(
                frame,
                raw_body=body,
                headers=headers,
                tail_event=tool_call_event(frame, signed_at=_signed_at(headers)),
                config=config,
                client=http(),
            )
            return JSONResponse(decision.answered.to_wire(), background=background)
        if frame.type != "prompt" or frame.is_connection_test():
            # No invocation, so no record: a pending one would have no
            # successor frame, and the flush would later push a console
            # connection test as a real invocation against a real user.
            log.info(
                "frame %s is %r/%s; allowing", webhook_id, frame.type, frame.source.application
            )
            return JSONResponse(ALLOW.to_wire(), background=background)

        # Built once and used twice: preflight's body is the tail event,
        # and the record this delivery writes for its fresh round is the
        # same object. Building it twice would hash the transcript twice
        # and could judge one invocation while storing another.
        signed_at = _signed_at(headers)
        tail_event = await unanswered_round(
            frame, webhook_id=webhook_id, signed_at=signed_at, config=config
        )
        decision: Decision = await decide(
            frame,
            raw_body=body,
            headers=headers,
            tail_event=tail_event,
            config=config,
            client=http(),
        )
        if store is not None:
            background.add_task(
                _write_safely,
                frame=frame,
                decision=decision,
                tail_event=tail_event,
                webhook_id=webhook_id,
                signed_at=signed_at,
                store=store,
                config=config,
                client=http(),
            )
        return JSONResponse(decision.answered.to_wire(), background=background)

    # Declared first: POST /{path:path} is a catch-all and Starlette matches
    # in declaration order, so a /tick declared after it is unreachable.
    @app.post("/tick")
    async def tick(request: Request, background: BackgroundTasks) -> Response:
        # A customer whose configured webhook URL happens to end in /tick
        # is a real collision: Anthropic posts to whatever path the admin
        # set and no suffix is reserved. A request carrying webhook-id is
        # therefore the delivery it claims to be, and takes the signature
        # path rather than this one.
        if "webhook-id" in request.headers:
            return await handle_frame(request, background)
        # Everything past here is the scheduler's route, and the token is
        # all that guards it: Cloud Run cannot scope an invoker to one
        # path, so a public service exposes this one too.
        token = _bearer(request.headers.get("authorization"))
        if token is None or not await authorize(token):
            log.warning("tick refused: no acceptable scheduler token")
            return Response(status_code=401)
        return JSONResponse(await run_tick())

    @app.post("/{path:path}")
    async def hook(request: Request, background: BackgroundTasks) -> Response:
        return await handle_frame(request, background)

    return app


def app() -> FastAPI:
    """uvicorn factory: ``uvicorn slashid_anthropic_forwarder.main:app --factory``.

    uvicorn configures only its own loggers, so without a root handler our
    INFO lines fall to Python's last-resort handler and are dropped.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    return create_app(config, backends=lambda: open_backends(config))
