"""Tests for the Converse S3 attachment resolver.

Migrated from ``bedrock/tests/test_s3.py::_resolve_s3_attachment branch coverage``
when the resolver moved into ``shared/normalize/converse/s3.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from slashid_ai_forwarder_core.normalize.converse import s3


@pytest.fixture(autouse=True)
def _reset_session() -> None:
    """Make sure no test leaks the cached aioboto3 session into another."""
    s3._get_session.cache_clear()


def _make_mock_s3_client(*, head_response: dict[str, Any] | None, get_body: bytes | None = None):
    """Return a mock async context manager that acts like an aioboto3 S3 client."""
    from botocore.exceptions import ClientError

    client = AsyncMock()

    if head_response is None:
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
        )
    else:
        client.head_object.return_value = head_response

    if get_body is not None:
        body_stream = AsyncMock()
        body_stream.read = AsyncMock(return_value=get_body)
        client.get_object.return_value = {"Body": body_stream}
    else:
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "GetObject"
        )

    # Make it work as an async context manager
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, client


@pytest.mark.asyncio
async def test_resolve_s3_attachment_head_fails_no_keys_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cm, _ = _make_mock_s3_client(head_response=None)
    monkeypatch.setattr(s3, "_get_session", lambda: MagicMock(client=lambda *_a, **_kw: cm))

    source: dict[str, Any] = {"s3Location": {"uri": "s3://bucket/key"}}
    await s3._resolve_s3_attachment(source, max_content_size=10 * 1024 * 1024)
    assert "_resolved_byte_length" not in source
    assert "_resolved_bytes" not in source


@pytest.mark.asyncio
async def test_resolve_s3_attachment_above_threshold_range_gets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Files larger than max_content_size trigger two Range GETs (head + tail)."""
    chunk = b"x" * 15  # each Range GET returns this
    cm, client = _make_mock_s3_client(
        head_response={"ContentLength": 200, "ContentType": "application/pdf"},
        get_body=chunk,
    )
    monkeypatch.setattr(s3, "_get_session", lambda: MagicMock(client=lambda *_a, **_kw: cm))

    source: dict[str, Any] = {"s3Location": {"uri": "s3://bucket/key"}}
    await s3._resolve_s3_attachment(source, max_content_size=20)  # 200 > 20

    assert source["_resolved_byte_length"] == 200
    assert source["_resolved_content_type"] == "application/pdf"
    assert "_resolved_bytes" not in source
    assert source["_resolved_head_bytes"] == chunk
    assert source["_resolved_tail_bytes"] == chunk
    assert client.get_object.call_count == 2
    # First call is a head Range, second is a tail Range
    calls = client.get_object.call_args_list
    assert "Range" in calls[0].kwargs
    assert calls[0].kwargs["Range"].startswith("bytes=0-")
    assert "Range" in calls[1].kwargs


@pytest.mark.asyncio
async def test_resolve_s3_attachment_within_threshold_fetches_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"file content"
    cm, _ = _make_mock_s3_client(
        head_response={"ContentLength": len(content), "ContentType": "text/plain"},
        get_body=content,
    )
    monkeypatch.setattr(s3, "_get_session", lambda: MagicMock(client=lambda *_a, **_kw: cm))

    source: dict[str, Any] = {"s3Location": {"uri": "s3://bucket/key"}}
    await s3._resolve_s3_attachment(source, max_content_size=10 * 1024 * 1024)

    assert source["_resolved_byte_length"] == len(content)
    assert source["_resolved_content_type"] == "text/plain"
    assert source["_resolved_bytes"] == content


@pytest.mark.asyncio
async def test_resolve_s3_attachment_empty_file_no_get(monkeypatch: pytest.MonkeyPatch) -> None:
    cm, client = _make_mock_s3_client(
        head_response={"ContentLength": 0, "ContentType": "application/octet-stream"}
    )
    monkeypatch.setattr(s3, "_get_session", lambda: MagicMock(client=lambda *_a, **_kw: cm))

    source: dict[str, Any] = {"s3Location": {"uri": "s3://bucket/empty"}}
    await s3._resolve_s3_attachment(source, max_content_size=10 * 1024 * 1024)

    assert source["_resolved_byte_length"] == 0
    assert source["_resolved_bytes"] == b""
    client.get_object.assert_not_called()


def test_parse_s3_uri_happy() -> None:
    assert s3._parse_s3_uri("s3://my-bucket/path/to/key.json") == ("my-bucket", "path/to/key.json")


@pytest.mark.parametrize("bad", ["", "https://example.com/x", "s3://", "s3:///key", "s3://bucket"])
def test_parse_s3_uri_rejects(bad: str) -> None:
    assert s3._parse_s3_uri(bad) is None
