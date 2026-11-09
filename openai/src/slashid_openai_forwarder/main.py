"""FastAPI entrypoint: the MCP hook on /mcp, its OAuth metadata, the upload
reader on /tick (or a local timer).

A hook call always gets a verdict back as a tool result. A non-result (an
HTTP error, ``isError``, a timeout) fails the hook on OpenAI's side, and a
failed hook does not block: answering is the only way to deny.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from slashid_ai_forwarder_core.platform import CheckpointStore, SchedulerAuth, TickLease
from slashid_ai_forwarder_core.sink import PreflightError, preflight_invocation

from . import hook, mcp
from .compliance import ComplianceClient
from .config import Config, load_config
from .directory import UserDirectory
from .oidc import TokenVerifier, Unauthenticated, discover_jwks_url, verifier
from .platform import Backends, open_backends
from .uploads import read_uploads

log = logging.getLogger(__name__)

TICK_LEASE = timedelta(minutes=9)
METADATA_PATH = "/.well-known/oauth-protected-resource"

try:
    VERSION = version("slashid-ai-forwarder-openai")
except PackageNotFoundError:  # pragma: no cover - a source checkout
    VERSION = "0.0.0"


async def _refuse(_token: str) -> bool:
    return False


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def create_app(
    config: Config,
    *,
    tokens: TokenVerifier | None = None,
    directory: UserDirectory | None = None,
    checkpoint: CheckpointStore | None = None,
    lease: TickLease | None = None,
    tick_auth: SchedulerAuth | None = None,
    client: httpx.AsyncClient | None = None,
    backends: Callable[[], contextlib.AbstractAsyncContextManager[Backends]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> FastAPI:
    """Every stateful piece is injected, or built at startup from ``config``;
    tests hand in fakes."""
    authorize = tick_auth or _refuse
    held: dict[str, httpx.AsyncClient | None] = {"client": client}

    def http() -> httpx.AsyncClient:
        if held["client"] is None:
            held["client"] = httpx.AsyncClient(timeout=config.request_timeout_seconds)
        return held["client"]

    def compliance() -> ComplianceClient | None:
        return ComplianceClient(config, http()) if config.compliance_enabled else None

    if directory is None and (api := compliance()) is not None:
        directory = UserDirectory(
            api.users,
            ttl_seconds=config.directory_ttl_seconds,
            miss_refresh_seconds=config.directory_miss_refresh_seconds,
        )

    async def run_tick() -> dict[str, Any]:
        api = compliance()
        if api is None or checkpoint is None:
            return {"log_files": 0, "uploads": 0}
        guard = lease.hold(TICK_LEASE) if lease is not None else contextlib.nullcontext(True)
        async with guard as leased:
            if not leased:
                return {"log_files": 0, "uploads": 0, "skipped": True}
            if directory is not None:
                # Keeps the hook's lookups off the listing path.
                await directory.warm()
            try:
                result = await read_uploads(
                    config=config,
                    compliance=api,
                    checkpoint=checkpoint,
                    http=http(),
                    now=datetime.now(UTC),
                )
            except Exception:
                # Never a failed tick: the watermark sits after the last
                # pushed file, and the next tick starts there.
                log.exception("tick: the upload reader failed")
                result = {"log_files": 0, "uploads": 0, "failed": True}
        log.info("tick: %s", result)
        return result

    async def tick_forever() -> None:
        while True:
            await asyncio.sleep(config.tick_interval_seconds)
            try:
                await run_tick()
            except Exception:
                log.exception("tick failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal checkpoint, lease, authorize, tokens

        async with contextlib.AsyncExitStack() as stack:
            if backends is not None:
                opened = await stack.enter_async_context(backends())
                checkpoint, lease, authorize = opened.checkpoint, opened.lease, opened.tick_auth
            if tokens is None and config.oidc_issuer and config.audience:
                jwks_url = config.oidc_jwks_url or await discover_jwks_url(
                    http(), config.oidc_issuer
                )
                tokens = verifier(
                    issuer=config.oidc_issuer,
                    audience=config.audience,
                    user_claim=config.oidc_user_claim,
                    jwks_url=jwks_url,
                )

            async def close_client() -> None:
                if client is None and held["client"] is not None:
                    await held["client"].aclose()

            stack.push_async_callback(close_client)
            if config.platform == "local":
                ticker = asyncio.create_task(tick_forever())

                async def stop_ticker() -> None:
                    ticker.cancel()
                    await asyncio.gather(ticker, return_exceptions=True)

                stack.push_async_callback(stop_ticker)
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def fail(event: hook.Event, cause: str) -> str:
        """No verdict: ``verdict_fail_mode`` decides. ``cause`` never carries
        the prompt or the tool's input."""
        if config.verdict_fail_mode == "allow":
            return hook.allow()
        return hook.deny(event, f"SlashID could not check this request: {cause}")

    async def verdict(call: hook.HookCall | mcp.UnreadableCall, email: str, deadline: float) -> str:
        if isinstance(call, mcp.UnreadableCall):
            log.warning("hook arguments did not parse: %d errors", call.errors)
            claimed: hook.Event = "PreToolUse" if call.event == "PreToolUse" else "UserPromptSubmit"
            return fail(claimed, "the hook's input template does not match slashid_preflight")
        event = call.hook_event_name
        if directory is None:
            return fail(event, "no ChatGPT workspace is configured")
        try:
            user_id = await asyncio.wait_for(
                directory.user_id(email), timeout=max(deadline - monotonic(), 0.0)
            )
        except TimeoutError:
            return fail(event, "the workspace directory did not answer in time")
        if user_id is None:
            log.warning("hook: no workspace user for the token's email")
            return fail(event, "this ChatGPT user is not in the workspace directory yet")
        try:
            invocation = hook.invocation(
                call,
                user_id=user_id,
                include_raw_content=config.include_raw_content,
                max_content_size=config.max_content_size,
            )
        except ValueError as exc:
            return fail(event, str(exc))
        remaining = deadline - monotonic()
        if remaining <= 0:
            return fail(event, "deadline exceeded")
        try:
            reasons = await preflight_invocation(
                http(),
                invocation,
                endpoint=config.endpoint,
                push_token=config.push_token,
                timeout_s=remaining,
            )
        except PreflightError as exc:
            log.warning("preflight %s failed: %s", invocation.request_id, exc)
            return fail(event, "preflight failed")
        return hook.deny(event, " ".join(reasons)) if reasons else hook.allow()

    def unauthorized() -> Response:
        challenge = "Bearer"
        if config.public_url:
            challenge += f' resource_metadata="{config.public_url}{METADATA_PATH}"'
        return Response(status_code=401, headers={"WWW-Authenticate": challenge})

    async def protected_resource() -> Response:
        if not (config.resource_url and config.oidc_issuer):
            return Response(status_code=404)
        return JSONResponse(
            {
                "resource": config.resource_url,
                "authorization_servers": [config.oidc_issuer],
                "bearer_methods_supported": ["header"],
                "resource_name": "SlashID AI policy check",
            }
        )

    app.add_api_route(METADATA_PATH, protected_resource, methods=["GET"])
    # RFC 9728's path-suffixed form, which a client derives from /mcp.
    app.add_api_route(f"{METADATA_PATH}/mcp", protected_resource, methods=["GET"])

    @app.post("/mcp")
    async def mcp_post(request: Request) -> Response:
        deadline = monotonic() + config.verdict_budget_seconds
        if tokens is None:
            return unauthorized()
        try:
            email = await tokens.user(request.headers.get("authorization"))
        except Unauthenticated as exc:
            log.info("mcp: refused: %s", exc)
            return unauthorized()
        try:
            message = json.loads(await request.body())
        except ValueError:
            return JSONResponse(mcp.error(None, mcp.PARSE_ERROR, "invalid JSON"), status_code=400)

        async def decide(call: hook.HookCall | mcp.UnreadableCall) -> str:
            return await verdict(call, email, deadline)

        response = await mcp.handle(message, decide, version=VERSION)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response)

    @app.get("/mcp")
    async def mcp_get() -> Response:
        # No server-initiated stream.
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/tick")
    async def tick(request: Request) -> Response:
        token = _bearer(request.headers.get("authorization"))
        if token is None or not await authorize(token):
            log.warning("tick refused: no acceptable scheduler token")
            return Response(status_code=401)
        return JSONResponse(await run_tick())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def app() -> FastAPI:
    """uvicorn factory: ``uvicorn slashid_openai_forwarder.main:app --factory``."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = load_config()
    return create_app(config, backends=lambda: open_backends(config))
