"""FastAPI entrypoint: Portkey's webhook guardrail on any path.

One rule shapes the handler: a call that carries a token is always answered
with a verdict. A non-200 is a webhook error to Portkey, which hands the
request to the guardrail's own fail-on-error setting, so whatever this
service cannot judge takes ``verdict_fail_mode`` here instead, and nothing
that runs after the verdict (the push) can change the response.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncIterator

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.sink import push_invocations

from .config import Config, load_config
from .event import after_events, before_event, identity_of
from .hook import Hook, parse_hook
from .verdict import ALLOW, Decision, decide, unjudged

log = logging.getLogger(__name__)


def _bearer(header: str | None) -> str | None:
    scheme, _, token = (header or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _parse(body: bytes) -> Hook | None:
    try:
        return parse_hook(json.loads(body))
    except ValueError:
        return None


async def _push_safely(
    client: httpx.AsyncClient, event: AIInvocationObservedV1, *, token: str, config: Config
) -> None:
    try:
        await push_invocations(
            client,
            [event],
            endpoint=config.endpoint,
            push_token=token,
            max_retries=config.max_retries,
        )
    except Exception:
        log.exception("push failed for %s", event.request_id)


def create_app(config: Config, *, client: httpx.AsyncClient | None = None) -> FastAPI:
    held: dict[str, httpx.AsyncClient | None] = {"client": client}

    def http() -> httpx.AsyncClient:
        # Lazily built and shared: ASGITransport runs no lifespan events,
        # so tests inject their own.
        if held["client"] is None:
            held["client"] = httpx.AsyncClient(timeout=config.request_timeout_seconds)
        return held["client"]

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if client is None and held["client"] is not None:
            await held["client"].aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def judge(hook: Hook, token: str, background: BackgroundTasks) -> Decision:
        identity = identity_of(hook.metadata, config)
        if identity is None:
            return unjudged(config, "the request's metadata names no user or agent")
        if hook.event_type == "beforeRequestHook":
            event = await before_event(hook, identity, config=config)
            if event is None:
                return unjudged(config, f"request type {hook.request_type!r} is not a chat")
            return await decide(event, token=token, config=config, client=http())
        after = await after_events(hook, identity, config=config)
        if after is None:
            return unjudged(config, f"request type {hook.request_type!r} is not a chat")
        if config.push_events:
            # The model ran and was paid for whatever the verdict on its
            # tool calls: the invocation is recorded either way.
            background.add_task(_push_safely, http(), after.record, token=token, config=config)
        if after.judged is None:
            return Decision(composed=ALLOW, answered=ALLOW)
        return await decide(after.judged, token=token, config=config, client=http())

    @app.post("/{path:path}")
    async def hook(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        if len(body) > config.max_body_bytes:
            return Response(status_code=413)
        token = _bearer(request.headers.get("authorization"))
        if token is None:
            return Response(status_code=401)
        parsed = _parse(body)
        if parsed is None:
            decision = unjudged(config, "the body is not a Portkey hook")
        else:
            try:
                decision = await judge(parsed, token, background)
            except Exception:
                log.exception("the hook could not be judged")
                decision = unjudged(config, "the hook could not be judged")
        return JSONResponse(decision.answered.to_wire(), background=background)

    return app


def app() -> FastAPI:
    """uvicorn factory: ``uvicorn slashid_portkey_forwarder.main:app --factory``.

    uvicorn configures only its own loggers, so without a root handler our
    INFO lines fall to Python's last-resort handler and are dropped.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return create_app(load_config())
