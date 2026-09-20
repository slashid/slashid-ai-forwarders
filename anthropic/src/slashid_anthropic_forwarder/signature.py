"""Standard Webhooks signature verification for Inference hooks requests.

https://www.standardwebhooks.com/ — HMAC-SHA256 over
``{webhook-id}.{webhook-timestamp}.{raw body bytes}``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence

TOLERANCE_SECONDS = 300


def verify(secrets: Sequence[str], headers: Mapping[str, str], body: bytes) -> bool:
    """True when ``body`` was signed by Anthropic under any of ``secrets``.

    ``body`` must be the raw bytes as received: a re-encoded JSON round trip
    produces a different digest and silently rejects everything. Anthropic
    sends header names lowercase, but proxies may re-case them.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    msg_id = lower.get("webhook-id")
    timestamp = lower.get("webhook-timestamp")
    signatures = lower.get("webhook-signature")
    if not (msg_id and timestamp and signatures):
        return False

    try:
        signed_at = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - signed_at) > TOLERANCE_SECONDS:
        return False

    payload = f"{msg_id}.{timestamp}.".encode() + body
    candidates = [c.encode() for c in signatures.split()]
    for secret in secrets:
        try:
            # Standard alphabet, not urlsafe: the secret routinely contains
            # '+' and '/', and a urlsafe decoder derives the wrong key bytes.
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
        except (ValueError, TypeError):
            continue
        expected = b"v1," + base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest())
        if any(hmac.compare_digest(expected, c) for c in candidates):
            return True
    return False
