"""Tests for the offloaded-body resolver."""

from __future__ import annotations

from typing import Any

import pytest

from slashid_bedrock_forwarder import mil_offload


@pytest.fixture(autouse=True)
def _reset_session() -> None:
    """Make sure no test leaks the cached aioboto3 session into another."""
    mil_offload._get_session.cache_clear()


def test_parse_s3_uri_happy() -> None:
    assert mil_offload._parse_s3_uri("s3://my-bucket/path/to/key.json") == (
        "my-bucket",
        "path/to/key.json",
    )


@pytest.mark.parametrize("bad", ["", "https://example.com/x", "s3://", "s3:///key", "s3://bucket"])
def test_parse_s3_uri_rejects(bad: str) -> None:
    assert mil_offload._parse_s3_uri(bad) is None


def test_decode_body_plain_json() -> None:
    assert mil_offload._decode_body(b'{"a": 1}', "key.json") == {"a": 1}


def test_decode_body_gzip() -> None:
    import gzip

    raw = gzip.compress(b'{"a": 2}')
    assert mil_offload._decode_body(raw, "key.json.gz") == {"a": 2}


def test_decode_body_invalid_returns_none() -> None:
    assert mil_offload._decode_body(b"not json", "key.json") is None


def test_decode_body_accepts_list_for_anthropic_streams() -> None:
    """InvokeModelWithResponseStream offloads land as top-level arrays of SSE events.

    Regression for B1: rejecting list bodies silently lost every large
    Anthropic streaming call's tool/stop-reason data.
    """
    raw = b'[{"type":"message_start"},{"type":"content_block_stop","index":0}]'
    decoded = mil_offload._decode_body(raw, "anthropic-stream.json")
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

    monkeypatch.setattr(mil_offload, "fetch_offloaded_body", fake_fetch)

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

    await mil_offload.resolve_offloaded_bodies(records)

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

    monkeypatch.setattr(mil_offload, "fetch_offloaded_body", fake_fetch)

    records = [{"input": {"inputBodyJson": None, "inputBodyS3Path": "s3://b/never"}}]
    await mil_offload.resolve_offloaded_bodies(records)
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

    monkeypatch.setattr(mil_offload, "fetch_offloaded_body", fake_fetch)

    # 50 offloaded records — uncapped, all 50 would race; capped, ≤ MAX_PARALLEL_FETCHES.
    records = [
        {"input": {"inputBodyJson": None, "inputBodyS3Path": f"s3://b/k{i}"}} for i in range(50)
    ]
    await mil_offload.resolve_offloaded_bodies(records)

    assert len(fetched) == 50  # everyone eventually runs
    assert max_in_flight <= mil_offload.MAX_PARALLEL_FETCHES, (
        f"concurrency cap breached: peaked at {max_in_flight}, "
        f"limit is {mil_offload.MAX_PARALLEL_FETCHES}"
    )
