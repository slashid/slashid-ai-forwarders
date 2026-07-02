"""Tests for the offloaded-body resolver."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from slashid_bedrock_forwarder import s3


@pytest.fixture(autouse=True)
def _reset_session() -> None:
    """Make sure no test leaks the cached aioboto3 session into another."""
    s3._get_session.cache_clear()


def test_parse_s3_uri_happy() -> None:
    assert s3._parse_s3_uri("s3://my-bucket/path/to/key.json") == ("my-bucket", "path/to/key.json")


@pytest.mark.parametrize("bad", ["", "https://example.com/x", "s3://", "s3:///key", "s3://bucket"])
def test_parse_s3_uri_rejects(bad: str) -> None:
    assert s3._parse_s3_uri(bad) is None


def test_decode_body_plain_json() -> None:
    assert s3._decode_body(b'{"a": 1}', "key.json") == {"a": 1}


def test_decode_body_gzip() -> None:
    import gzip

    raw = gzip.compress(b'{"a": 2}')
    assert s3._decode_body(raw, "key.json.gz") == {"a": 2}


def test_decode_body_invalid_returns_none() -> None:
    assert s3._decode_body(b"not json", "key.json") is None


def test_decode_body_accepts_list_for_anthropic_streams() -> None:
    """InvokeModelWithResponseStream offloads land as top-level arrays of SSE events.

    Regression for B1: rejecting list bodies silently lost every large
    Anthropic streaming call's tool/stop-reason data.
    """
    raw = b'[{"type":"message_start"},{"type":"content_block_stop","index":0}]'
    decoded = s3._decode_body(raw, "anthropic-stream.json")
    assert isinstance(decoded, list)
    assert len(decoded) == 2


@pytest.mark.asyncio
async def test_resolve_offloaded_bodies_inlines_fetched_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched: list[str] = []

    async def fake_fetch(uri: str, *, max_attempts: int = 6) -> dict[str, Any] | None:
        fetched.append(uri)
        return {"toolConfig": {"tools": [{"toolSpec": {"name": "Bash"}}]}}

    monkeypatch.setattr(s3, "fetch_offloaded_body", fake_fetch)

    records: list[dict[str, Any]] = [
        {
            "input": {"inputBodyJson": None, "inputBodyS3Path": "s3://b/k1"},
            "output": {"outputBodyJson": None, "outputBodyS3Path": "s3://b/k2"},
        },
        {
            "input": {"inputBodyJson": {"already": "inline"}, "inputBodyS3Path": "s3://b/skip"},
            "output": {"outputBodyJson": {"already": "inline"}},
        },
    ]

    await s3.resolve_offloaded_bodies(records)

    # First record's two offloaded bodies were fetched, second record's
    # inline bodies were left untouched.
    assert set(fetched) == {"s3://b/k1", "s3://b/k2"}
    assert records[0]["input"]["inputBodyJson"] == {
        "toolConfig": {"tools": [{"toolSpec": {"name": "Bash"}}]}
    }
    assert records[0]["output"]["outputBodyJson"] == {
        "toolConfig": {"tools": [{"toolSpec": {"name": "Bash"}}]}
    }
    assert records[1]["input"]["inputBodyJson"] == {"already": "inline"}


@pytest.mark.asyncio
async def test_resolve_offloaded_bodies_tolerates_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(uri: str, *, max_attempts: int = 6) -> dict[str, Any] | None:
        return None  # body never landed

    monkeypatch.setattr(s3, "fetch_offloaded_body", fake_fetch)

    records = [{"input": {"inputBodyJson": None, "inputBodyS3Path": "s3://b/never"}}]
    await s3.resolve_offloaded_bodies(records)
    # Body stays None — downstream just sees an empty toolConfig and emits
    # an event with no available_tools, not a hard failure.
    assert records[0]["input"]["inputBodyJson"] is None


@pytest.mark.asyncio
async def test_resolve_offloaded_bodies_caps_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for R2: a large CW Logs batch with many offloads must not
    spawn unbounded S3 GETs — Lambda's thread pool and the boto3 connection
    pool both top out around ~10."""
    import asyncio

    in_flight = 0
    max_in_flight = 0
    fetched: list[str] = []

    async def fake_fetch(uri: str, *, max_attempts: int = 6) -> dict[str, Any] | None:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        # Yield so peers schedule and the counter actually piles up.
        await asyncio.sleep(0.01)
        fetched.append(uri)
        in_flight -= 1
        return {"ok": True}

    monkeypatch.setattr(s3, "fetch_offloaded_body", fake_fetch)

    # 50 offloaded records — uncapped, all 50 would race; capped, ≤ MAX_PARALLEL_FETCHES.
    records = [
        {"input": {"inputBodyJson": None, "inputBodyS3Path": f"s3://b/k{i}"}} for i in range(50)
    ]
    await s3.resolve_offloaded_bodies(records)

    assert len(fetched) == 50  # everyone eventually runs
    assert max_in_flight <= s3.MAX_PARALLEL_FETCHES, (
        f"concurrency cap breached: peaked at {max_in_flight}, limit is {s3.MAX_PARALLEL_FETCHES}"
    )


# ---------------------------------------------------------------------------
# _resolve_s3_attachment branch coverage
# ---------------------------------------------------------------------------


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
