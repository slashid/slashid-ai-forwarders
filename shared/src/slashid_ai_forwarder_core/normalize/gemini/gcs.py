"""Fetch Vertex Gemini fileData attachment metadata from GCS.

For ``fileData`` parts referencing ``gs://bucket/object`` URIs, this
module issues ``download_metadata`` (HEAD) to get byte length + md5 +
content-type. When ``include_raw_content`` is enabled and the object
fits ``max_content_size``, an additional ``download`` (GET) fetches the
full body so the caller can compute sha256/sha1/md5 locally and
populate ``redacted_content``. Oversized files fall back to two
``download(..., headers={"Range": "..."})`` calls (head + tail chunks)
so ``truncate_middle`` can produce a well-formed elided snippet — no
hash on partial fetch.

All GCS calls use ``gcloud-aio-storage`` for native async I/O and pick
up Application Default Credentials from the Cloud Function's runtime
service account. The dependency is optional: ``gcloud-aio-storage`` is
only pulled in when the ``[gcs]`` extras group is installed (via
``slashid-ai-forwarder-core[gcs]`` — the Vertex forwarder does this).
Non-Vertex forwarders (Bedrock) don't need it.

GCS returns ``md5Hash`` as base64; this module converts it to lowercase
hex on the stash so downstream wire assembly matches the inline / S3
formatting (``{"md5": "<hex>"}`` on ``AIAccessedFile.content_hashes``).
CRC32C is not emitted — the 32-bit checksum is unsafe as a dedup key at
~65k-file birthday-collision granularity.
"""

from __future__ import annotations

import base64
import logging
from functools import cache

from gcloud.aio.storage import Storage

from ...content_utils import SNAP_TOLERANCE

log = logging.getLogger(__name__)


# Cap concurrent GCS calls within one Cloud Function invocation.
# Matches ``converse/s3.py::MAX_PARALLEL_FETCHES``. The two paths run
# against different object stores with different cost curves but keeping
# the same limit avoids surprising the operator.
MAX_PARALLEL_FETCHES = 8


@cache
def _get_client() -> Storage:
    return Storage()


def _parse_gs_uri(uri: str) -> tuple[str, str] | None:
    if not uri.startswith("gs://"):
        return None
    bucket, _, key = uri[len("gs://") :].partition("/")
    if not bucket or not key:
        return None
    return bucket, key


def _md5_b64_to_hex(b64: str | None) -> str | None:
    """GCS ``md5Hash`` is base64. Convert to lowercase hex to match the
    inline / S3 wire format on ``AIAccessedFile.content_hashes``."""
    if not b64:
        return None
    try:
        return base64.b64decode(b64).hex()
    except Exception:
        return None


async def _resolve_gcs_attachment(
    source: dict[str, object],
    *,
    max_content_size: int,
    include_raw_content: bool,
) -> None:
    """HEAD + optional GET a Gemini fileData source.

    Stashes on the source dict:
      ``_resolved_byte_length``  — object size from download_metadata
      ``_resolved_md5_hex``      — md5Hash converted from base64 to hex
      ``_resolved_content_type`` — contentType from download_metadata
      ``_resolved_bytes``        — full body when opt-in and size ≤ cap
      ``_resolved_head_bytes``   — first chunk when file exceeds cap (opt-in only)
      ``_resolved_tail_bytes``   — last chunk when file exceeds cap (opt-in only)

    Nothing is stashed when the source has no ``gs://`` URI or when
    ``download_metadata`` fails — the caller falls through to a stub
    ``AIAccessedFile`` matching the Phase 3.1 shape.

    All keys are read by ``extract_attachments`` in ``attachments.py``.
    """
    from aiohttp import ClientResponseError

    uri = source.get("fileUri") or ""
    if not isinstance(uri, str):
        return
    parsed = _parse_gs_uri(uri)
    if parsed is None:
        return
    bucket, key = parsed

    async with _get_client() as client:
        try:
            meta = await client.download_metadata(bucket, key)
        except ClientResponseError:
            return
        except Exception:  # network / auth errors — same fallback
            log.debug("gcs download_metadata failed for %s", uri, exc_info=True)
            return

        try:
            size = int(meta.get("size", 0))
        except (TypeError, ValueError):
            return
        source["_resolved_byte_length"] = size
        if md5_hex := _md5_b64_to_hex(meta.get("md5Hash")):
            source["_resolved_md5_hex"] = md5_hex
        if ct := meta.get("contentType"):
            source["_resolved_content_type"] = ct

        if not include_raw_content:
            return

        if size == 0:
            source["_resolved_bytes"] = b""
            return
        if size <= max_content_size:
            try:
                body = await client.download(bucket, key)
                source["_resolved_bytes"] = body
            except Exception:
                log.debug("gcs download failed for %s", uri, exc_info=True)
            return

        # Oversized: two Range GETs — enough for ``truncate_middle`` with snap tolerance.
        # gcloud-aio-storage's Storage.download accepts a ``headers`` kwarg;
        # a Range header there triggers a partial GET (verified against
        # gcloud-aio-storage 9.6.x; no separate download_range method exists).
        chunk = max_content_size // 2 + SNAP_TOLERANCE
        try:
            head_bytes = await client.download(
                bucket,
                key,
                headers={"Range": f"bytes=0-{chunk - 1}"},
            )
            source["_resolved_head_bytes"] = head_bytes
            tail_start = max(size - chunk, 0)
            tail_bytes = await client.download(
                bucket,
                key,
                headers={"Range": f"bytes={tail_start}-{size - 1}"},
            )
            source["_resolved_tail_bytes"] = tail_bytes
        except Exception:
            log.debug("gcs range download failed for %s", uri, exc_info=True)
