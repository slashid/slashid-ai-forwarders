"""FastAPI entrypoint: one POST route at any path, signature gate, verdict.

Owns rule 1 of the design: nothing that happens after the verdict is
decided (capture today, the event push later) may change the response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .capture import Capture, GcsCapture
from .config import Config, load_config
from .signature import verify

log = logging.getLogger(__name__)

ALLOW = {"action": "allow"}


def _reference_id(webhook_id: str) -> str:
    # Same recipe as the Go policy receiver, so every record joins on one value.
    return hashlib.sha256(webhook_id.encode()).hexdigest()[:32]


async def _capture_safely(capture: Capture, request_id: str, headers: dict, body: bytes) -> None:
    try:
        await capture.store(request_id, headers, body)
    except Exception:
        log.exception("capture failed for %s", request_id)


def create_app(config: Config, *, capture: Capture | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if capture is None and config.capture_bucket:
        capture = GcsCapture(config.capture_bucket)

    @app.post("/{path:path}")
    async def hook(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        if len(body) > config.max_body_bytes:
            return Response(status_code=413)
        headers = {k: v for k, v in request.headers.items()}
        if not verify(config.signing_secrets, headers, body) and not config.hook_allow_unsigned:
            return Response(status_code=401)
        webhook_id = headers.get("webhook-id", "")

        if capture is not None:
            background.add_task(_capture_safely, capture, webhook_id, headers, body)

        try:
            frame = json.loads(body)
            frame_type = frame.get("type") if isinstance(frame, dict) else None
        except ValueError:
            frame_type = None
        if frame_type != "prompt":
            log.warning("frame %s has type %r; allowing", webhook_id, frame_type)
            return JSONResponse(ALLOW, background=background)

        marker = config.capture_deny_marker
        if marker and marker.encode() in body:
            verdict = {
                "action": "deny",
                "deny_reason": "Denied by the SlashID capture test marker.",
                "reference_id": _reference_id(webhook_id),
            }
            log.info("verdict for %s: %s (enforce=%s)", webhook_id, verdict, config.enforce)
            if config.enforce:
                return JSONResponse(verdict, background=background)
        return JSONResponse(ALLOW, background=background)

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
    return create_app(load_config())
