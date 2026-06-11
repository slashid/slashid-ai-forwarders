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
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .config import Config, load_config
from .events import build_event
from .mil_normalize import normalize_record
from .s3 import resolve_offloaded_bodies
from .sink import push_invocations

log = logging.getLogger()
log.setLevel(logging.INFO)


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
    """Pull MIL records out of `logEvents[].message`, normalize on the way through.

    `messageType=CONTROL_MESSAGE` heartbeats are skipped silently.
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
            records.append(normalize_record(record))
    return records


async def _run(records: list[dict[str, Any]], config: Config) -> dict[str, int]:
    """Resolve any offloaded MIL bodies, build events, push them in batches."""
    await resolve_offloaded_bodies(records)

    events = [
        built
        for r in records
        if (built := build_event(r, include_raw_content=config.include_raw_content)) is not None
    ]

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
