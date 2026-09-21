"""Attachment enrichment — the one thing a frame can never supply.

A frame's ``attachment`` block carries extracted text and no bytes, so
the hook side has nothing to hash but that text: exact for plain text,
and unmatchable for anything Claude stored as a processed copy. The
compliance ``files[]`` listing carries the stored file's ``md5``, and
its content endpoint streams the stored bytes.

Two tiers:

* ``md5`` (default) — no extra request, no file bytes through the
  collector, and the digest is already in a response Reader B fetches
  anyway. Not a lesser tier: Salesforce-sourced graph resources carry
  md5 alone.
* ``full`` — one GET per attachment, adding sha1 and sha256 for
  OneDrive, SharePoint and Drive.

Every digest here is of **what Claude stored**, which is not always what
the user uploaded: a processed image, or a document kept as extracted
text. Such a hash will not match the original, and the README says so.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.normalized.media_types import parse_media_type

from ..config import Config
from .client import ComplianceClient, ComplianceError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ListedFile:
    """One ``files[]`` entry. The only cheap metadata there is: ``HEAD``
    on the content endpoint 404s on every attachment."""

    id: str
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    md5: str | None


def listed_files(message: Mapping[str, Any]) -> list[ListedFile]:
    """The uploads hanging off one message.

    ``files[]`` hangs off the single message that carried the upload, so
    an attachment is reported once, on the invocation that consumed it.
    Without that, a twenty-turn chat about one PDF would look like twenty
    accesses. ``generated_files`` and ``artifacts`` are deliberately not
    read here: the first is reserved for a ``generated`` provenance that
    is not emitted yet, and an artifact is the assistant's own output,
    which does not belong in a field a reviewer reads as ingress.
    """
    raw = message.get("files")
    if not isinstance(raw, Sequence):
        return []
    out: list[ListedFile] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            continue
        digest = entry.get("md5")
        out.append(
            ListedFile(
                id=entry["id"],
                filename=entry.get("filename"),
                mime_type=entry.get("mime_type"),
                size_bytes=entry.get("size_bytes"),
                # Lowercase hex on the wire; normalized so a comparison
                # against a recomputed digest cannot fail on case.
                md5=digest.lower() if isinstance(digest, str) else None,
            )
        )
    return out


async def files_from_listing(
    client: ComplianceClient, entries: Iterable[ListedFile], *, config: Config
) -> list[AIAccessedFile]:
    """One ``AIAccessedFile`` per listed upload, at the configured tier."""
    out: list[AIAccessedFile] = []
    for entry in entries:
        hashes: dict[str, str] | None = {"md5": entry.md5} if entry.md5 else None
        if _should_fetch(entry, config):
            try:
                data = await client.file_content(entry.id)
            except ComplianceError as exc:
                # Degrade to the listing's md5 rather than losing the
                # entry: a whole-file digest we already hold is still
                # matchable, and this is enrichment, not the record.
                log.warning("compliance: attachment %s not fetched (%s)", entry.id, exc)
            else:
                hashes = {
                    "md5": hashlib.md5(data).hexdigest(),
                    "sha1": hashlib.sha1(data).hexdigest(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
        out.append(
            AIAccessedFile(
                name=entry.filename,
                content_hashes=hashes,
                # A recorded `mime_type` is sometimes a bare extension
                # ("txt"), which is not a media type; Chunk 2 made this
                # fall back to None rather than raise.
                media_type=parse_media_type(entry.mime_type),
                byte_length=entry.size_bytes,
                provenance="attachment",
            )
        )
    return out


def _should_fetch(entry: ListedFile, config: Config) -> bool:
    """Decided from the listing, before any request.

    There is no ``HEAD`` to size the object with, so ``size_bytes`` is
    the only pre-fetch signal there is. An unknown size is treated as
    over the cap: starting a fetch we cannot bound and then stopping it
    would leave a partial read, and a partial read is never hashed.
    """
    if config.attachment_hashing != "full":
        return False
    if entry.size_bytes is None:
        log.info("compliance: attachment %s has no size_bytes; keeping the listing md5", entry.id)
        return False
    return entry.size_bytes <= config.max_attachment_fetch_bytes
