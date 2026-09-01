"""Lambda entry point.

CloudWatch Logs subscription filters deliver events shaped like:

    {"awslogs": {"data": "<base64-encoded gzipped JSON>"}}

The decoded payload contains `logEvents[]`, each holding one MIL record
serialized in the `message` field. The pipeline:

  1. Decode + decompress + json-parse the CW Logs payload
  2. Normalize each record (Anthropic → Converse shape)
  3. Build AIInvocationObservedV1 events → POST /nhi/events/ai-invocations

The connection ID is supplied via env var (the customer's streaming
endpoint), and the only credential is the connection's push bearer
token. Identity creation, role-chain unrolling, and conversation
stitching are SlashID-side responsibilities.

Failure modes are raised; AWS retries the async invocation twice, then
the CloudWatch `Errors` metric increments and the event is dropped.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field
from slashid_ai_forwarder_core.events import AIInvocationObservedV1, build_event
from slashid_ai_forwarder_core.s3 import resolve_offloaded_bodies
from slashid_ai_forwarder_core.sink import push_invocations

from .config import Config, load_config
from .mil_normalize import normalize_record

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())


class CWLogsAwsLogs(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data: str


class CWLogsEvent(BaseModel):
    """The base64-gzipped CloudWatch Logs subscription delivery envelope."""

    model_config = ConfigDict(extra="ignore")
    awslogs: CWLogsAwsLogs


class CWLogEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str | None = None
    timestamp: int | None = None
    message: str


class CWLogsPayload(BaseModel):
    """The decoded JSON inside `awslogs.data`."""

    model_config = ConfigDict(extra="ignore")
    messageType: str | None = None
    logEvents: list[CWLogEntry] = Field(default_factory=list)


def _decode_cw_payload(event: CWLogsEvent) -> CWLogsPayload:
    """Decode the base64-encoded gzipped CW Logs payload."""
    compressed = base64.b64decode(event.awslogs.data)
    raw = gzip.decompress(compressed)
    return CWLogsPayload.model_validate_json(raw)


def _records_from_payload(payload: CWLogsPayload) -> list[dict[str, Any]]:
    """Pull MIL records out of `logEvents[].message`.

    `messageType=CONTROL_MESSAGE` heartbeats are skipped silently. Body
    normalization happens in `_run` after offloaded bodies are fetched
    from S3 — running it here would no-op on offloaded records (the body
    is `null` until S3 resolution lands).
    """
    if payload.messageType == "CONTROL_MESSAGE":
        return []

    records: list[dict[str, Any]] = []
    for entry in payload.logEvents:
        try:
            record = json.loads(entry.message)
        except json.JSONDecodeError:
            log.warning("skipping non-JSON log line: %r", entry.message[:120])
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


_REDACTED_FIELDS = {"redacted_text", "redacted_content"}


def _log_event(event: AIInvocationObservedV1) -> None:
    """Log the event as JSON, stripping redacted_text / redacted_content."""
    raw = event.model_dump(mode="json", exclude_none=True)

    def _strip(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: _strip(v) for k, v in obj.items() if k not in _REDACTED_FIELDS}
        if isinstance(obj, list):
            return [_strip(i) for i in obj]
        return obj

    log.info("event: %s", json.dumps(_strip(raw), separators=(",", ":")))


async def _run(records: list[dict[str, Any]], config: Config) -> dict[str, int]:
    """Resolve offloaded MIL bodies, normalize, build + push events."""
    await resolve_offloaded_bodies(records)
    for record in records:
        normalize_record(record)

    built_or_none = await asyncio.gather(
        *(
            build_event(
                r,
                include_raw_content=config.include_raw_content,
                max_content_size=config.max_content_size,
            )
            for r in records
        )
    )
    events = [e for e in built_or_none if e is not None]
    for e in events:
        _log_event(e)

    timeout = httpx.Timeout(config.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        event_count = await push_invocations(
            client,
            events,
            endpoint=config.endpoint,
            push_token=config.push_token,
            max_retries=config.max_retries,
        )

    return {"events_pushed": event_count, "records_seen": len(records)}


def lambda_handler(event: dict[str, Any], context: object) -> dict[str, int]:
    """Lambda entry point.

    `event` is the raw AWS Lambda payload (a dict); we validate it into a
    `CWLogsEvent` here so AWS's invocation signature stays dict-shaped.
    """
    del context
    config = load_config()
    parsed_event = CWLogsEvent.model_validate(event)
    payload = _decode_cw_payload(parsed_event)
    records = _records_from_payload(payload)
    if not records:
        log.info("no MIL records in payload (control message or empty batch)")
        return {"events_pushed": 0, "records_seen": 0}

    return asyncio.run(_run(records, config))
