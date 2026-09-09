"""Fetch Bedrock Converse document/image attachment metadata from S3.

For document/image blocks with S3 sources (Converse ``s3Location``), this
module issues HeadObject to get byte length and, when the object is within
the configured inline threshold, GetObject to populate content hash and
optionally raw bytes.

All S3 calls use aioboto3 for native async I/O. The dependency is optional:
``aioboto3`` is only pulled in when the ``[converse]`` extras group is
installed (via ``slashid-ai-forwarder-core[converse]`` — bedrock does this).
Non-Converse forwarders (Vertex) don't need it.
"""

from __future__ import annotations

import logging
from functools import cache
from typing import Any

import aioboto3

from ...content_utils import SNAP_TOLERANCE

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


async def _resolve_s3_attachment(source: dict[str, Any], *, max_content_size: int) -> None:
    """HEAD + optional GET a Converse s3Location source block.

    Stashes on the source dict:
      `_resolved_byte_length`   — ContentLength from HeadObject
      `_resolved_content_type`  — ContentType from HeadObject (media_type fallback)
      `_resolved_bytes`         — full body when size == 0 or size <= max_content_size bytes
      `_resolved_head_bytes`    — first chunk when file exceeds max_content_size (no hash)
      `_resolved_tail_bytes`    — last chunk when file exceeds max_content_size (no hash)

    When the file is larger than max_content_size, two Range GETs fetch
    enough bytes for _truncate_middle to produce a well-formed snippet.
    No content_hash is set for partial fetches since the bytes are incomplete.

    All keys are read by ``extract_attachments`` in ``attachments.py``.
    """
    from botocore.exceptions import ClientError

    # Converse shape: source.s3Location.uri  — Bedrock Playground: source.s3Uri
    s3_loc = source.get("s3Location") or {}
    uri = s3_loc.get("uri") or source.get("s3Uri") or ""
    parsed = _parse_s3_uri(uri)
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
        if ct := head.get("ContentType"):
            source["_resolved_content_type"] = ct

        if size == 0:
            source["_resolved_bytes"] = b""
        elif size <= max_content_size:
            try:
                resp = await s3.get_object(Bucket=bucket, Key=key)
                source["_resolved_bytes"] = await resp["Body"].read()
            except ClientError:
                pass
        else:
            # Fetch head and tail chunks — enough for _truncate_middle with snap tolerance.
            chunk = max_content_size // 2 + SNAP_TOLERANCE
            try:
                head_resp = await s3.get_object(
                    Bucket=bucket, Key=key, Range=f"bytes=0-{chunk - 1}"
                )
                source["_resolved_head_bytes"] = await head_resp["Body"].read()
                tail_start = max(size - chunk, 0)
                tail_resp = await s3.get_object(
                    Bucket=bucket, Key=key, Range=f"bytes={tail_start}-{size - 1}"
                )
                source["_resolved_tail_bytes"] = await tail_resp["Body"].read()
            except ClientError:
                pass
