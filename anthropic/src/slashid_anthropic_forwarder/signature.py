"""Standard Webhooks signature verification for Inference hooks requests.

A thin wrapper over the reference implementation
(https://www.standardwebhooks.com/), which owns the crypto: HMAC-SHA256
over ``{webhook-id}.{webhook-timestamp}.{raw body bytes}``, the ``whsec_``
prefix, the standard base64 alphabet, the ±300 s tolerance and
constant-time comparison. The wrapper adds the two things the library
does not do — accept more than one secret, so a rotation can be ridden
out, and answer with a bool rather than an exception.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache

from standardwebhooks import Webhook, WebhookVerificationError


@lru_cache(maxsize=8)
def _verifiers(secrets: tuple[str, ...]) -> tuple[Webhook, ...]:
    """One verifier per usable secret, built once per secret set.

    Construction decodes the secret and raises on a malformed one. Skip
    those rather than let a mistyped second entry reject traffic the
    first entry would have accepted.
    """
    built: list[Webhook] = []
    for secret in secrets:
        try:
            built.append(Webhook(secret))
        except Exception:  # misconfigured secret, not a request fault
            continue
    return tuple(built)


def verify(secrets: Sequence[str], headers: Mapping[str, str], body: bytes) -> bool:
    """True when ``body`` was signed by Anthropic under **any** of ``secrets``.

    Any number is accepted, tried in order. Two is what a rotation needs;
    more is allowed and costs one failed HMAC each, which is why the
    caller, not this function, decides whether an empty list may pass.

    ``body`` must be the raw bytes as received: hashing a re-encoded JSON
    round trip produces a different digest and silently rejects
    everything. ``json_parse=False`` keeps the library from parsing a
    megabyte-scale frame whose parse we would immediately discard.
    """
    for webhook in _verifiers(tuple(secrets)):
        try:
            webhook.verify(body, dict(headers), json_parse=False)
            return True
        except WebhookVerificationError:
            continue
    return False
