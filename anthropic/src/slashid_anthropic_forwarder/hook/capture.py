"""Raw-frame capture for protocol study on test tenants.

Every frame is stored byte-for-byte with its headers so the wire shapes
Anthropic actually sends (message runs, attachments, post-denial rounds)
can be turned into fixtures. Never enable on a customer tenant.
"""

from __future__ import annotations

import json
import time

from slashid_ai_forwarder_core.platform import BlobSink


async def capture_frame(
    sink: BlobSink, request_id: str, headers: dict[str, str], body: bytes
) -> None:
    """One object per delivery: ``<unix-ms>_<webhook-id>.json`` holding
    ``{"headers": {...}, "body": <raw utf-8 body>}``."""
    name = f"{int(time.time() * 1000)}_{request_id}.json"
    payload = json.dumps(
        {"headers": headers, "body": body.decode("utf-8", errors="replace")},
        ensure_ascii=False,
    )
    await sink.put(name, payload.encode(), content_type="application/json")
