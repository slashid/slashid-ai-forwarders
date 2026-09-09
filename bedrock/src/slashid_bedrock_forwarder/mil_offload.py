"""Fetch MIL body-offload objects from S3.

For large invocations (Claude Code's system prompt is the canonical case)
MIL writes the main record to CloudWatch Logs with `inputBodyJson: null`
and `inputBodyS3Path: "s3://bucket/key"` pointing to a separate S3 object
that follows ~30-60s later. This module fetches the offloaded bodies
and merges them back into the record so downstream code can read the
`toolConfig` / tool definitions uniformly.

Attachment resolution for document/image blocks (Converse ``s3Location``)
lives in ``shared/normalize/converse/s3.py`` — it's vendor-format-specific
and rides on the shared aioboto3 dependency.

All S3 calls use aioboto3 for native async I/O.
"""

from __future__ import annotations

import gzip
import json
import logging
from functools import cache
from typing import Any

import aioboto3
from slashid_ai_forwarder_core.normalize._fetch_semaphore import get_fetch_semaphore
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

log = logging.getLogger(__name__)


@cache
def _get_session() -> aioboto3.Session:
    return aioboto3.Session()


def _parse_s3_uri(uri: str) -> tuple[str, str] | None:
    if not uri.startswith("s3://"):
        return None
    bucket, _, key = uri[len("s3://") :].partition("/")
    if not bucket or not key:
        return None
    return bucket, key


def _decode_body(raw: bytes, key: str) -> dict[str, Any] | list[Any] | None:
    """Gunzip if needed, JSON-parse, return a dict, list, or None on failure.

    Anthropic-shape outputs from InvokeModelWithResponseStream are a
    top-level *list* of SSE events; mil_normalize handles that shape in
    `_reconstruct_message_from_stream`. Rejecting lists here would
    silently lose every large InvokeModel-against-Anthropic call.
    """
    if key.endswith(".gz"):
        raw = gzip.decompress(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("offloaded body at %s is not valid JSON", key)
        return None
    return parsed if isinstance(parsed, dict | list) else None


class _OffloadedBodyNotReady(Exception):
    """Body object hasn't been written yet — retryable."""


async def fetch_offloaded_body(
    s3_uri: str, *, max_attempts: int = 6
) -> dict[str, Any] | list[Any] | None:
    """Fetch + decode a body-offload object, retrying while it lands.

    Bedrock writes the body-offload object after the main MIL record by
    up to ~60 seconds. We retry on NoSuchKey every 15s for 6 attempts
    (~90s total window) to cover the worst-case lag. Returns None on
    permanent failure — callers should treat that as "body unavailable"
    rather than abort.
    """
    parsed = _parse_s3_uri(s3_uri)
    if parsed is None:
        log.warning("malformed inputBodyS3Path / outputBodyS3Path: %r", s3_uri)
        return None
    bucket, key = parsed

    from botocore.exceptions import ClientError

    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_fixed(15),
        retry=retry_if_exception_type(_OffloadedBodyNotReady),
        reraise=True,
    )
    try:
        async for attempt in retrying:
            with attempt:
                async with _get_session().client("s3") as s3:
                    try:
                        resp = await s3.get_object(Bucket=bucket, Key=key)
                    except ClientError as e:
                        code = e.response.get("Error", {}).get("Code", "")
                        if code in ("NoSuchKey", "NotFound", "404"):
                            raise _OffloadedBodyNotReady(
                                f"s3://{bucket}/{key} not yet available"
                            ) from e
                        raise
                    raw = await resp["Body"].read()
                    return _decode_body(raw, key)
    except _OffloadedBodyNotReady as e:
        log.warning("offloaded body still missing after %d attempts: %s", max_attempts, e)
        return None
    except Exception as e:
        log.warning("failed to fetch offloaded body %s: %s", s3_uri, e)
        return None
    return None  # unreachable but ty wants the explicit return


async def resolve_offloaded_bodies(records: list[dict[str, Any]]) -> None:
    """Mutate each record in place, inlining any offloaded bodies.

    Records with `inputBodyJson` / `outputBodyJson` already inline are
    left untouched. Records pointing at S3 paths get the fetched body
    merged into the same field; the path field stays so downstream code
    can still see it was offloaded.

    All fetches run concurrently — for a batch of N records with offloads,
    one event loop turn issues N parallel S3 GETs.
    """
    import asyncio

    tasks: list[tuple[dict[str, Any], str, str]] = []  # (input/output dict, field, s3 path)

    for record in records:
        inp = record.get("input")
        if isinstance(inp, dict) and inp.get("inputBodyJson") is None:
            path = inp.get("inputBodyS3Path")
            if isinstance(path, str) and path:
                tasks.append((inp, "inputBodyJson", path))

        out = record.get("output")
        if isinstance(out, dict) and out.get("outputBodyJson") is None:
            path = out.get("outputBodyS3Path")
            if isinstance(path, str) and path:
                tasks.append((out, "outputBodyJson", path))

    if not tasks:
        return

    sem = get_fetch_semaphore()

    async def _guarded(path: str) -> dict[str, Any] | list[Any] | None:
        async with sem:
            return await fetch_offloaded_body(path)

    results = await asyncio.gather(
        *(_guarded(path) for _, _, path in tasks),
        return_exceptions=False,
    )

    for (container, field, path), body in zip(tasks, results, strict=True):
        if body is not None:
            container[field] = body
        else:
            log.warning("proceeding without offloaded body for %s", path)
