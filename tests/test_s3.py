"""Tests for the offloaded-body resolver."""

from __future__ import annotations

from typing import Any

import pytest

from slashid_bedrock_forwarder import s3


@pytest.fixture(autouse=True)
def _reset_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure no test leaks the cached boto3 client into another."""
    monkeypatch.setattr(s3, "_s3_client", None)


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
