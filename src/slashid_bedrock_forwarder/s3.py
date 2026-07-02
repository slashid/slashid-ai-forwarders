"""Fetch MIL body-offload objects and file attachments from S3.

For large invocations (Claude Code's system prompt is the canonical case)
MIL writes the main record to CloudWatch Logs with `inputBodyJson: null`
and `inputBodyS3Path: "s3://bucket/key"` pointing to a separate S3 object
that follows ~30-60s later. This module fetches the offloaded bodies
and merges them back into the record so downstream code can read the
`toolConfig` / tool definitions uniformly.

For document/image blocks with S3 sources (Converse `s3Location`), this
module issues HeadObject to get byte length and, when the object is within
the configured inline threshold, GetObject to populate content hash and
optionally raw bytes.

All S3 calls use aioboto3 for native async I/O.
"""

from __future__ import annotations

import gzip
import json
import logging
from typing import Any

import aioboto3
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

log = logging.getLogger(__name__)


# Cap concurrent S3 calls within one Lambda invocation.
MAX_PARALLEL_FETCHES = 8

_session: aioboto3.Session | None = None


def _get_session() -> aioboto3.Session:
    global _session
    if _session is None:
        _session = aioboto3.Session()
    return _session


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


async def _resolve_s3_attachment(source: dict[str, Any], *, max_inline_bytes: int) -> None:
    """HEAD + optional GET a Converse s3Location source block.

    Adds `_resolved_byte_length` from HeadObject. When the object is within
    the inline threshold, also adds `_resolved_bytes` from GetObject.
    Both keys are read by `_accessed_files` in events.py.
    """
    from botocore.exceptions import ClientError

    s3_loc = source.get("s3Location") or {}
    parsed = _parse_s3_uri(s3_loc.get("uri") or "")
    if parsed is None:
        return
    bucket, key = parsed

    async with _get_session().client("s3") as s3:
        try:
            head = await s3.head_object(Bucket=bucket, Key=key)
        except ClientError:
            return
        size = int(head["ContentLength"])
        source["_resolved_byte_length"] = size

        if max_inline_bytes > 0 and size <= max_inline_bytes:
            try:
                resp = await s3.get_object(Bucket=bucket, Key=key)
                source["_resolved_bytes"] = await resp["Body"].read()
            except ClientError:
                pass


def _iter_attachment_sources(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect S3-sourced document/image source blocks from the current turn.

    Only looks at messages after the last assistant message — files from
    earlier turns were already reported in prior invocations.
    """
    sources: list[dict[str, Any]] = []
    for record in records:
        body = (record.get("input") or {}).get("inputBodyJson")
        if not isinstance(body, dict):
            continue
        messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
        last_assistant = max(
            (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
            default=-1,
        )
        for msg in messages[last_assistant + 1 :]:
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                for key in ("document", "image"):
                    item = block.get(key)
                    if not isinstance(item, dict):
                        continue
                    src = item.get("source") or {}
                    if "s3Location" in src and "_resolved_byte_length" not in src:
                        sources.append(src)
    return sources


async def resolve_file_attachments(records: list[dict[str, Any]], *, max_inline_bytes: int) -> None:
    """HEAD (and optionally GET) every S3-sourced document/image block.

    Results are stashed as `_resolved_byte_length` / `_resolved_bytes` on
    the source block. Runs concurrently under the same MAX_PARALLEL_FETCHES
    cap as body offloads.
    """
    import asyncio

    sources = _iter_attachment_sources(records)
    if not sources:
        return

    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

    async def _guarded(src: dict[str, Any]) -> None:
        async with sem:
            await _resolve_s3_attachment(src, max_inline_bytes=max_inline_bytes)

    await asyncio.gather(*(_guarded(s) for s in sources))


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

    sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

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
            log.info("proceeding without offloaded body for %s", path)
