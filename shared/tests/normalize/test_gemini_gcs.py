"""Tests for the Gemini GCS attachment resolver.

Mirrors ``test_converse_s3.py`` — mocks the gcloud-aio-storage client
via AsyncMock so tests don't need real GCS credentials or network.
"""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from slashid_ai_forwarder_core.normalize.gemini import gcs


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    """Prevent one test's mock client from leaking into another."""
    gcs._get_client.cache_clear()


def _b64_md5(raw: bytes) -> str:
    """Encode an md5 digest the way GCS does — base64 (not hex)."""
    import hashlib
    return base64.b64encode(hashlib.md5(raw).digest()).decode()


def _make_mock_gcs_client(
    *,
    metadata: dict[str, Any] | None,
    download_return: bytes | list[bytes] | None = None,
    download_raises: bool = False,
):
    """Return an AsyncMock + async context manager acting like gcloud.aio.storage.Storage.

    ``metadata``        — dict returned by ``download_metadata`` (or None to raise 404).
    ``download_return`` — either bytes for a single call, or a list of bytes for
                          multiple calls in order (Range GETs land as sequential
                          ``download(bucket, key, headers={"Range": "bytes=..."})``
                          invocations). ``None`` leaves ``download`` un-stubbed.
    ``download_raises`` — if True, ``download`` raises ClientResponseError(403).

    ``gcloud.aio.storage.Storage`` exposes a single ``download`` method for both
    full and ranged reads (range = ``headers={"Range": "bytes=X-Y"}`` kwarg) — no
    separate ``download_range``. The fixture reflects that by side-effecting the
    same method across calls.
    """
    from aiohttp import ClientResponseError
    from yarl import URL

    def _resp_err(status: int) -> ClientResponseError:
        return ClientResponseError(
            request_info=MagicMock(real_url=URL("https://storage.googleapis.com/x/y")),
            history=(),
            status=status,
            message="mock",
        )

    client = AsyncMock()

    if metadata is None:
        client.download_metadata.side_effect = _resp_err(404)
    else:
        client.download_metadata.return_value = metadata

    if download_raises:
        client.download.side_effect = _resp_err(403)
    elif isinstance(download_return, list):
        client.download.side_effect = list(download_return)
    elif download_return is not None:
        client.download.return_value = download_return

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client


@pytest.mark.asyncio
async def test_resolve_gcs_attachment_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default path: HEAD succeeds → byte_length + md5 (hex) + content_type stashed."""
    raw = b"hello world"
    cm, _ = _make_mock_gcs_client(
        metadata={
            "size": str(len(raw)),
            "md5Hash": _b64_md5(raw),
            "contentType": "text/plain",
        },
    )
    monkeypatch.setattr(gcs, "_get_client", lambda: cm)

    source: dict[str, Any] = {"fileUri": "gs://bucket/hello.txt"}
    await gcs._resolve_gcs_attachment(
        source, max_content_size=10 * 1024 * 1024, include_raw_content=False,
    )
    assert source["_resolved_byte_length"] == len(raw)
    # md5 stashed in hex (converted from GCS's base64 encoding).
    import hashlib
    assert source["_resolved_md5_hex"] == hashlib.md5(raw).hexdigest()
    assert source["_resolved_content_type"] == "text/plain"
    assert "_resolved_bytes" not in source


@pytest.mark.asyncio
async def test_resolve_gcs_attachment_head_fails_no_keys_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD 404 / 403 leaves the source dict untouched → downstream emits stub."""
    cm, _ = _make_mock_gcs_client(metadata=None)
    monkeypatch.setattr(gcs, "_get_client", lambda: cm)

    source: dict[str, Any] = {"fileUri": "gs://bucket/missing.pdf"}
    await gcs._resolve_gcs_attachment(
        source, max_content_size=10 * 1024 * 1024, include_raw_content=False,
    )
    assert "_resolved_byte_length" not in source
    assert "_resolved_md5_hex" not in source
    assert "_resolved_bytes" not in source


@pytest.mark.asyncio
async def test_resolve_gcs_attachment_within_threshold_fetches_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in + size ≤ cap → GET body stashed under _resolved_bytes."""
    raw = b"file contents"
    cm, _ = _make_mock_gcs_client(
        metadata={
            "size": str(len(raw)),
            "md5Hash": _b64_md5(raw),
            "contentType": "text/plain",
        },
        download_return=raw,
    )
    monkeypatch.setattr(gcs, "_get_client", lambda: cm)

    source: dict[str, Any] = {"fileUri": "gs://bucket/file.txt"}
    await gcs._resolve_gcs_attachment(
        source, max_content_size=10 * 1024 * 1024, include_raw_content=True,
    )
    assert source["_resolved_byte_length"] == len(raw)
    assert source["_resolved_bytes"] == raw


@pytest.mark.asyncio
async def test_resolve_gcs_attachment_above_threshold_range_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in + oversized → two ``download(..., headers={"Range": ...})`` calls
    for head + tail chunks."""
    head_chunk = b"H" * 15
    tail_chunk = b"T" * 15
    cm, client = _make_mock_gcs_client(
        metadata={
            "size": "200",
            "md5Hash": _b64_md5(b"unused"),
            "contentType": "application/pdf",
        },
        download_return=[head_chunk, tail_chunk],
    )
    monkeypatch.setattr(gcs, "_get_client", lambda: cm)

    source: dict[str, Any] = {"fileUri": "gs://bucket/large.pdf"}
    await gcs._resolve_gcs_attachment(
        source, max_content_size=20, include_raw_content=True,
    )
    assert source["_resolved_byte_length"] == 200
    assert source["_resolved_content_type"] == "application/pdf"
    assert "_resolved_bytes" not in source
    assert source["_resolved_head_bytes"] == head_chunk
    assert source["_resolved_tail_bytes"] == tail_chunk
    assert client.download.call_count == 2
    # First call = head Range starting at byte 0; second = tail Range.
    calls = client.download.call_args_list
    assert calls[0].kwargs["headers"]["Range"].startswith("bytes=0-")
    assert calls[1].kwargs["headers"]["Range"].startswith("bytes=")
    assert not calls[1].kwargs["headers"]["Range"].startswith("bytes=0-")


@pytest.mark.asyncio
async def test_resolve_gcs_attachment_include_raw_disabled_no_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """include_raw_content=False → never call download,
    even for small files. Metadata-only is the default path."""
    raw = b"small file"
    cm, client = _make_mock_gcs_client(
        metadata={
            "size": str(len(raw)),
            "md5Hash": _b64_md5(raw),
            "contentType": "text/plain",
        },
        download_return=raw,
    )
    monkeypatch.setattr(gcs, "_get_client", lambda: cm)

    source: dict[str, Any] = {"fileUri": "gs://bucket/x"}
    await gcs._resolve_gcs_attachment(
        source, max_content_size=10 * 1024 * 1024, include_raw_content=False,
    )
    client.download.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_gcs_attachment_empty_file_no_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-byte file: skip download, stash empty _resolved_bytes when opt-in."""
    cm, client = _make_mock_gcs_client(
        metadata={
            "size": "0",
            "md5Hash": _b64_md5(b""),
            "contentType": "application/octet-stream",
        },
    )
    monkeypatch.setattr(gcs, "_get_client", lambda: cm)

    source: dict[str, Any] = {"fileUri": "gs://bucket/empty"}
    await gcs._resolve_gcs_attachment(
        source, max_content_size=10 * 1024 * 1024, include_raw_content=True,
    )
    assert source["_resolved_byte_length"] == 0
    assert source["_resolved_bytes"] == b""
    client.download.assert_not_called()


def test_parse_gs_uri_happy() -> None:
    assert gcs._parse_gs_uri("gs://my-bucket/path/to/key.pdf") == ("my-bucket", "path/to/key.pdf")


@pytest.mark.parametrize(
    "bad",
    ["", "https://example.com/x", "s3://bucket/key", "gs://", "gs:///key", "gs://bucket"],
)
def test_parse_gs_uri_rejects(bad: str) -> None:
    assert gcs._parse_gs_uri(bad) is None


def test_md5_b64_to_hex_round_trip() -> None:
    """The public helper must survive an md5 digest that GCS returns as base64."""
    import hashlib
    raw = b"canonical dedup key"
    expected = hashlib.md5(raw).hexdigest()
    got = gcs._md5_b64_to_hex(base64.b64encode(hashlib.md5(raw).digest()).decode())
    assert got == expected
