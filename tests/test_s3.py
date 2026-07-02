"""Tests for the offloaded-body resolver."""

from __future__ import annotations

from typing import Any

import pytest

from slashid_bedrock_forwarder import s3


@pytest.fixture(autouse=True)
def _reset_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no test leaks the cached aioboto3 session into another."""
    monkeypatch.setattr(s3, "_session", None)


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


def _record_with_s3_doc(uri: str) -> dict[str, Any]:
    return {
        "input": {
            "inputBodyJson": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "document": {
                                    "name": "report.pdf",
                                    "format": "pdf",
                                    "source": {"s3Location": {"uri": uri}},
                                }
                            },
                        ],
                    }
                ]
            }
        }
    }


@pytest.mark.asyncio
async def test_resolve_file_attachments_populates_size_and_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"PDF content here"

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
        source["_resolved_byte_length"] = len(content)
        if max_inline_bytes >= len(content):
            source["_resolved_bytes"] = content

    monkeypatch.setattr(s3, "_resolve_s3_attachment", fake_resolve)

    records = [_record_with_s3_doc("s3://bucket/report.pdf")]
    await s3.resolve_file_attachments(records, max_inline_bytes=10 * 1024 * 1024)

    src = records[0]["input"]["inputBodyJson"]["messages"][0]["content"][0]["document"]["source"]
    assert src["_resolved_byte_length"] == len(content)
    assert src["_resolved_bytes"] == content


@pytest.mark.asyncio
async def test_resolve_file_attachments_skips_inline_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inline (bytes) sources have no s3Location — nothing should be fetched."""
    called: list[Any] = []

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
        called.append(source)

    monkeypatch.setattr(s3, "_resolve_s3_attachment", fake_resolve)

    records = [
        {
            "input": {
                "inputBodyJson": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "document": {
                                        "name": "f.txt",
                                        "format": "txt",
                                        "source": {"bytes": "aGk="},
                                    }
                                }
                            ],
                        }
                    ]
                }
            }
        }
    ]
    await s3.resolve_file_attachments(records, max_inline_bytes=10 * 1024 * 1024)
    assert called == []


@pytest.mark.asyncio
async def test_resolve_file_attachments_only_current_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 attachments before the last assistant message are not fetched."""
    called_uris: list[str] = []

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
        uri = (source.get("s3Location") or {}).get("uri", "")
        called_uris.append(uri)

    monkeypatch.setattr(s3, "_resolve_s3_attachment", fake_resolve)

    records = [
        {
            "input": {
                "inputBodyJson": {
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "document": {
                                        "name": "old.pdf",
                                        "format": "pdf",
                                        "source": {"s3Location": {"uri": "s3://b/old.pdf"}},
                                    }
                                }
                            ],
                        },
                        {"role": "assistant", "content": [{"text": "ok"}]},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "document": {
                                        "name": "new.pdf",
                                        "format": "pdf",
                                        "source": {"s3Location": {"uri": "s3://b/new.pdf"}},
                                    }
                                }
                            ],
                        },
                    ]
                }
            }
        }
    ]
    await s3.resolve_file_attachments(records, max_inline_bytes=10 * 1024 * 1024)
    assert called_uris == ["s3://b/new.pdf"]


@pytest.mark.asyncio
async def test_resolve_file_attachments_skips_already_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sources with `_resolved_byte_length` already set are not re-fetched."""
    called: list[Any] = []

    async def fake_resolve(source: dict[str, Any], *, max_inline_bytes: int) -> None:
        called.append(source)

    monkeypatch.setattr(s3, "_resolve_s3_attachment", fake_resolve)

    records = [_record_with_s3_doc("s3://bucket/report.pdf")]
    # Pre-populate
    src = records[0]["input"]["inputBodyJson"]["messages"][0]["content"][0]["document"]["source"]
    src["_resolved_byte_length"] = 42

    await s3.resolve_file_attachments(records, max_inline_bytes=10 * 1024 * 1024)
    assert called == []
