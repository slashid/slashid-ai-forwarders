"""Attachment enrichment: the listing's md5, or the stored bytes."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import httpx

from slashid_anthropic_forwarder.compliance.attachments import (
    files_from_listing,
    listed_files,
)
from slashid_anthropic_forwarder.compliance.client import ComplianceClient
from tests.compliance_fixtures import MARIA_BYTES, body, transport
from tests.test_pending import config as a_config


def _uploads() -> dict[str, Any]:
    return next(m for m in body("chat_messages_1.json")["chat_messages"] if m.get("files"))


def a_client() -> tuple[ComplianceClient, list[httpx.Request]]:
    client, seen = transport()
    return ComplianceClient(client, api_key="k"), seen


def test_listed_files_reads_the_five_fields_and_lowercases_the_digest() -> None:
    entries = listed_files(_uploads())
    assert [e.filename for e in entries] == [
        "guiaSADT.pdf",
        "WhatsApp Image 2026-09-02 at 17.07.21.jpeg",
        "maria.txt",
    ]
    assert all(e.md5 == (e.md5 or "").lower() for e in entries)
    assert listed_files({"role": "user", "content": []}) == []


async def test_md5_tier_takes_the_listing_digest_and_makes_no_request() -> None:
    client, seen = a_client()
    entries = listed_files(_uploads())
    files = await files_from_listing(client, entries, config=a_config())
    assert seen == []
    assert [set(f.content_hashes or {}) for f in files] == [{"md5"}] * 3
    assert all(f.provenance == "attachment" for f in files)
    assert [f.byte_length for f in files] == [59430, 72878, 27]


async def test_full_tier_downloads_and_its_md5_equals_the_listing() -> None:
    client, seen = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.filename == "maria.txt")
    files = await files_from_listing(client, [entry], config=a_config(attachment_hashing="full"))
    assert len(seen) == 1
    hashes = files[0].content_hashes or {}
    assert set(hashes) == {"md5", "sha1", "sha256"}
    # The listing's md5 was recorded from the tenant; these bytes are the
    # frame's extracted text. Their agreeing is the measured claim.
    assert hashes["md5"] == entry.md5 == hashlib.md5(MARIA_BYTES).hexdigest()
    assert hashes["sha256"] == hashlib.sha256(MARIA_BYTES).hexdigest()


async def test_an_oversized_file_is_never_requested() -> None:
    # Decided from the listing's size_bytes before any fetch — there is no
    # HEAD to fall back on, so this is the only place to decide it.
    client, seen = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.mime_type == "image/jpeg")
    files = await files_from_listing(
        client,
        [entry],
        config=a_config(attachment_hashing="full", max_attachment_fetch_bytes=1024),
    )
    assert seen == []
    assert files[0].content_hashes == {"md5": entry.md5}


async def test_an_unknown_size_is_treated_as_over_the_cap() -> None:
    client, seen = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.filename == "maria.txt")
    # `replace`, not a dict splat of `__dict__`: the splat hands every
    # field back as `Any` and ty cannot see that `id` is still a str.
    sized = replace(entry, size_bytes=None)
    files = await files_from_listing(client, [sized], config=a_config(attachment_hashing="full"))
    assert seen == []
    assert files[0].content_hashes == {"md5": entry.md5}


async def test_a_missing_body_degrades_to_the_listing_md5() -> None:
    client, _ = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.filename == "guiaSADT.pdf")
    files = await files_from_listing(client, [entry], config=a_config(attachment_hashing="full"))
    # The corpus holds no body for the PDF, so the transport 404s and the
    # entry keeps a whole-file digest instead of losing the entry.
    assert files[0].content_hashes == {"md5": entry.md5}


async def test_a_bare_extension_mime_type_degrades_to_none() -> None:
    # `"txt"` is not a media type. Chunk 2 Task 2.2 made parse_media_type
    # fall back rather than raise; this is the recorded value that needs it.
    client, _ = a_client()
    message = next(m for m in body("chat_messages_2.json")["chat_messages"] if m.get("files"))
    files = await files_from_listing(client, listed_files(message), config=a_config())
    assert files[0].media_type is None
    assert files[0].content_hashes is not None
