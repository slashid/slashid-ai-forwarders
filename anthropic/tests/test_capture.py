"""``BlobCapture``: one object per delivery, whatever the platform's sink."""

from __future__ import annotations

import json

from slashid_anthropic_forwarder.hook.capture import BlobCapture


class Sink:
    def __init__(self) -> None:
        self.put_calls: list[tuple[str, bytes, str]] = []

    async def put(self, name: str, data: bytes, *, content_type: str) -> None:
        self.put_calls.append((name, data, content_type))


async def test_a_frame_is_stored_raw_with_its_headers() -> None:
    sink = Sink()
    body = '{"type": "prompt", "note": "café"}'.encode()
    await BlobCapture(sink).store("wh_1", {"webhook-id": "wh_1"}, body)
    [(name, data, content_type)] = sink.put_calls
    assert name.endswith("_wh_1.json")
    assert content_type == "application/json"
    stored = json.loads(data)
    assert stored == {"headers": {"webhook-id": "wh_1"}, "body": body.decode()}
