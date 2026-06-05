"""Lambda entry point.

CloudWatch Logs subscription filters deliver events shaped like:

    {"awslogs": {"data": "<base64-encoded gzipped JSON>"}}

The decoded payload contains `logEvents[]`, each holding one MIL record
serialized in the `message` field. The pipeline:

  1. Decode + decompress + json-parse the CW Logs payload
  2. Normalize each record (Anthropic → Converse shape)
  3. Extract unique identities → POST /nhi/identities/import
  4. Discover the manual_import connection (cached for the container lifetime)
  5. Build AIInvocationObservedV1 events → POST /nhi/events/ai-invocations

Failure modes are raised; the async-invocation DLQ catches terminal failures.
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
from .events import build_event, extract_identities
from .mil_normalize import normalize_record
from .sink import (
    discover_manual_import_connection,
    import_identities,
    push_invocations,
)

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


# Container-lifetime cache for the discovered manual_import connection.
_connection_cache: tuple[str, str] | None = None


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


async def _resolve_connection(client: httpx.AsyncClient, config: Config) -> tuple[str, str]:
    """Return (connection_id, push_token), reading from the cache when warm."""
    global _connection_cache
    if _connection_cache is None:
        _connection_cache = await discover_manual_import_connection(
            client,
            endpoint=config.endpoint,
            admin_token=config.admin_token,
            org_id=config.org_id,
            max_retries=config.max_retries,
        )
    return _connection_cache


async def _run(records: list[dict[str, Any]], config: Config) -> dict[str, int]:
    """Identity import → connection discovery → AI invocation push."""
    timeout = httpx.Timeout(config.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        identities = extract_identities(records)
        identity_count = await import_identities(
            client,
            identities,
            endpoint=config.endpoint,
            admin_token=config.admin_token,
            org_id=config.org_id,
            max_retries=config.max_retries,
        )

        connection_id, push_token = await _resolve_connection(client, config)

        events = [
            built
            for r in records
            if (
                built := build_event(
                    r,
                    org_id=config.org_id,
                    connection_id=connection_id,
                    identity_source_type=config.identity_source_type,
                )
            )
            is not None
        ]
        event_count = await push_invocations(
            client,
            events,
            endpoint=config.endpoint,
            push_token=push_token,
            max_retries=config.max_retries,
        )

    return {
        "identities_imported": identity_count,
        "events_pushed": event_count,
        "records_seen": len(records),
    }


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
        return {"identities_imported": 0, "events_pushed": 0, "records_seen": 0}

    return asyncio.run(_run(records, config))
