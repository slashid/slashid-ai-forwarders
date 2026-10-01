"""The per-user daemon: routes and guards, the collection worker, the watchdog
and the idle timer (spec "Architecture")."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import logging.handlers
import os
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from slashid_ai_forwarder_core.platform.local import create_local_platform
from starlette.middleware.base import RequestResponseEndpoint

from .cache import SessionCache
from .config import CodexConfig
from .discovery import (
    INFO_FILE,
    LOCK_FILE,
    LOG_FILE,
    DaemonInfo,
    DaemonLock,
    acquire_lock,
    clear_spawn_failure,
    config_digest,
    ensure_state_dir,
    hmac_response,
    package_version,
    remove_daemon_json,
    write_daemon_json,
)
from .emit import Collector, Trigger, Worker
from .handler import DAEMON_ERROR, PREFLIGHT_EVENTS, TRIGGER_EVENTS, Handler
from .http import make_client
from .install import created_at
from .mcp_servers import McpServers
from .preflight import Preflight, Verdict, fail_verdict
from .sink import CodexSink
from .state import SqliteFileRecordStore, connect

log = logging.getLogger(__name__)

TICK_S = 1.0
WATCHDOG_STALE_S = 10.0
LOCK_WAIT_S = 3.0
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3
MAX_NONCE_CHARS = 128
# ``run_daemon`` exit codes; losing the lock to a live daemon is not a failure.
EXIT_OK, EXIT_CONFIG = 0, 2


@dataclass
class Services:
    """What the routes call; injected so tests can fake them."""

    # (event, payload, arrival) → the verdict.
    preflight: Callable[[str, bytes, float], Awaitable[Verdict]]
    enqueue_trigger: Callable[[str, bytes], None]
    clock: Callable[[], float] = time.monotonic
    exit: Callable[[int], None] = os._exit
    # Delete ``daemon.json``, release the lock, stop serving.
    on_shutdown: Callable[[], None] = lambda: None


class Lifetime:
    """The idle clock, reset by hooks and published batches only, and the
    event loop's heartbeat."""

    def __init__(self, clock: Callable[[], float], idle_seconds: float) -> None:
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._active_at = self._beat_at = clock()

    def touch(self) -> None:
        self._active_at = self._clock()

    def beat(self) -> None:
        self._beat_at = self._clock()

    def idle_expired(self) -> bool:
        return self._clock() - self._active_at >= self._idle_seconds

    def stale(self) -> bool:
        return self._clock() - self._beat_at > WATCHDOG_STALE_S


class Watchdog:
    """Exits the process when the loop's heartbeat goes stale, so a hung
    daemon releases its lock and the next hook replaces it."""

    def __init__(
        self, lifetime: Lifetime, exit: Callable[[int], None], *, interval: float = TICK_S
    ) -> None:
        self._lifetime = lifetime
        self._exit = exit
        self._interval = interval
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="codex-watchdog", daemon=True)

    def check(self) -> bool:
        if not self._lifetime.stale():
            return False
        log.error("event loop heartbeat stale; exiting")
        self._exit(1)
        return True

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def _run(self) -> None:
        while not self._stopped.wait(self._interval):
            if self.check():
                return


async def tick(
    lifetime: Lifetime, on_idle: Callable[[], None], *, interval: float = TICK_S
) -> None:
    """Beats the heartbeat; calls ``on_idle`` once when the idle clock expires."""
    idle = False
    while True:
        lifetime.beat()
        if not idle and lifetime.idle_expired():
            idle = True
            log.info("idle; exiting")
            on_idle()
        await asyncio.sleep(interval)


def create_app(config: CodexConfig, secret: str, port: int, services: Services) -> FastAPI:
    lifetime = Lifetime(services.clock, config.daemon_idle_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(tick(lifetime, services.on_shutdown))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    app.state.lifetime = lifetime
    host = f"127.0.0.1:{port}"
    bearer = f"Bearer {secret}".encode()

    @app.middleware("http")
    async def guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        # DNS rebinding and browsers.
        if request.headers.getlist("host") != [host] or "origin" in request.headers:
            return Response(status_code=403)
        if request.url.path != "/ping":
            sent = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(sent, bearer):
                return Response(status_code=401)
        return await call_next(request)

    @app.get("/ping")
    async def ping(nonce: Annotated[str, Query(max_length=MAX_NONCE_CHARS)]) -> Response:
        return PlainTextResponse(hmac_response(secret, nonce))

    @app.post("/hooks/{event}")
    async def hook(event: str, request: Request) -> Response:
        arrival = time.monotonic()
        lifetime.touch()
        if event in PREFLIGHT_EVENTS:
            payload = await request.body()
            try:
                verdict = await services.preflight(event, payload, arrival)
            except Exception:
                log.exception("preflight %s failed", event)
                verdict = fail_verdict(config, DAEMON_ERROR)
            return JSONResponse(verdict.model_dump(exclude_none=True))
        if event in TRIGGER_EVENTS:
            payload = await request.body()
            try:
                services.enqueue_trigger(event, payload)
            except Exception:
                log.exception("trigger %s failed", event)
            return JSONResponse({})
        return Response(status_code=404)

    @app.post("/shutdown")
    async def shutdown() -> Response:
        services.on_shutdown()
        return JSONResponse({})

    return app


def lifetime_of(app: FastAPI) -> Lifetime:
    lifetime = app.state.lifetime
    assert isinstance(lifetime, Lifetime)
    return lifetime


# --------------------------------------------------------------------------
# The process
# --------------------------------------------------------------------------


@dataclass
class Stopper:
    """Deletes ``daemon.json`` then releases the lock, once, then stops the
    server. Holding the lock until the file is gone keeps a successor's file
    safe from deletion."""

    state_dir: Path
    lock: DaemonLock
    server: uvicorn.Server | None = None
    _done: bool = field(default=False, init=False)
    _mutex: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __call__(self) -> None:
        with self._mutex:
            if not self._done:
                self._done = True
                remove_daemon_json(self.state_dir)
                self.lock.release()
        if self.server is not None:
            self.server.should_exit = True


def setup_logging(path: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def run_daemon(config_path: Path, *, state_dir: Path, codex_home: Path | None = None) -> int:
    ensure_state_dir(state_dir)
    setup_logging(state_dir / LOG_FILE)
    log.info("daemon %s starting, pid %d", package_version(), os.getpid())
    try:
        config = CodexConfig.load(config_path)
    except (OSError, ValueError) as exc:
        log.error("config %s unusable: %s", config_path, exc)
        return EXIT_CONFIG
    if codex_home is not None:
        config = config.model_copy(update={"codex_home": codex_home})
    lock = acquire_lock(state_dir / LOCK_FILE, wait=LOCK_WAIT_S)
    if lock is None:
        log.info("another daemon holds %s; exiting", LOCK_FILE)
        return EXIT_OK
    stopper = Stopper(state_dir, lock)
    try:
        _serve(config, config_path, state_dir, stopper)
    finally:
        stopper()
    log.info("daemon stopped")
    return EXIT_OK


def _serve(config: CodexConfig, config_path: Path, state_dir: Path, stopper: Stopper) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port: int = sock.getsockname()[1]
    secret = secrets.token_hex(32)

    created = created_at(state_dir)
    records = SqliteFileRecordStore(lambda: connect(state_dir))
    cache = SessionCache(codex_home=config.codex_home)
    client = make_client(timeout_seconds=config.request_timeout_seconds)
    preflight = Preflight(config, CodexSink(config, client), records, cache)
    worker: Worker | None = None

    def submit(trigger: Trigger) -> None:
        if worker is not None:
            worker.submit(trigger)

    handler = Handler(config, preflight, cache, submit)
    services = Services(
        preflight=handler.preflight, enqueue_trigger=handler.trigger, on_shutdown=stopper
    )
    app = create_app(config, secret, port, services)
    lifetime = lifetime_of(app)

    @asynccontextmanager
    async def open_collector() -> AsyncIterator[Collector]:
        async with (
            create_local_platform(state_dir) as platform,
            make_client(timeout_seconds=config.request_timeout_seconds) as push_client,
        ):
            yield Collector(
                config,
                CodexSink(config, push_client),
                records,
                platform,
                cache,
                created_at=created,
                mcp=McpServers(config.codex_bin),
                on_published=lifetime.touch,
            )

    worker = Worker(open_collector)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on"))
    stopper.server = server
    info = DaemonInfo(port, secret, os.getpid(), package_version(), config_digest(config_path))
    write_daemon_json(state_dir, info)
    clear_spawn_failure(state_dir)
    log.info("listening on 127.0.0.1:%d; %s written", port, INFO_FILE)
    # Hooks wait in the listen backlog until the server runs.
    worker.start()
    watchdog = Watchdog(lifetime, services.exit)
    watchdog.start()
    try:
        asyncio.run(_run_server(server, sock, client))
    finally:
        watchdog.stop()
        worker.stop()


async def _run_server(
    server: uvicorn.Server, sock: socket.socket, client: httpx.AsyncClient
) -> None:
    try:
        await server.serve(sockets=[sock])
    finally:
        await client.aclose()
