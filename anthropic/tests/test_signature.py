"""Standard Webhooks signature verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from slashid_anthropic_forwarder.signature import verify

# 0xfb 0xff 0xbf encodes as "+/+/", so the secret carries both characters
# a URL-safe decoder gets wrong.
SECRET = "whsec_" + base64.b64encode(bytes([0xFB, 0xFF, 0xBF]) * 8).decode()


def _sign(secret: str, msg_id: str, ts: str, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    payload = f"{msg_id}.{ts}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode()


def _headers(msg_id: str, ts: str, sig: str) -> dict[str, str]:
    return {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": sig}


def _now() -> str:
    return str(int(time.time()))


def test_valid_signature_accepted() -> None:
    body, ts = b'{"type":"prompt"}', _now()
    assert verify([SECRET], _headers("msg_1", ts, _sign(SECRET, "msg_1", ts, body)), body)


def test_tampered_body_rejected() -> None:
    ts = _now()
    sig = _sign(SECRET, "msg_1", ts, b'{"type":"prompt"}')
    assert not verify([SECRET], _headers("msg_1", ts, sig), b'{"type":"evil"}')


def test_secret_with_urlsafe_chars_verifies() -> None:
    assert "+" in SECRET or "/" in SECRET
    body, ts = b"{}", _now()
    assert verify([SECRET], _headers("m", ts, _sign(SECRET, "m", ts, body)), body)


def test_stale_timestamp_rejected() -> None:
    body, ts = b"{}", str(int(time.time()) - 400)
    assert not verify([SECRET], _headers("m", ts, _sign(SECRET, "m", ts, body)), body)


def test_future_timestamp_rejected() -> None:
    body, ts = b"{}", str(int(time.time()) + 400)
    assert not verify([SECRET], _headers("m", ts, _sign(SECRET, "m", ts, body)), body)


def test_unsigned_rejected() -> None:
    assert not verify([SECRET], {}, b"{}")


def _secret(seed: int) -> str:
    return "whsec_" + base64.b64encode(bytes([seed]) * 32).decode()


def test_second_secret_accepted_during_rotation() -> None:
    other = _secret(1)
    body, ts = b"{}", _now()
    assert verify([SECRET, other], _headers("m", ts, _sign(other, "m", ts, body)), body)


def test_any_one_of_many_secrets_verifies() -> None:
    """The set is not capped at the two a rotation needs: every secret is
    tried and the match may be the last."""
    others = [_secret(i) for i in range(1, 5)]
    body, ts = b"{}", _now()
    sig = _sign(others[-1], "m", ts, body)
    assert verify([SECRET, *others], _headers("m", ts, sig), body)


def test_no_matching_secret_rejects() -> None:
    body, ts = b"{}", _now()
    sig = _sign(_secret(9), "m", ts, body)
    assert not verify([SECRET, _secret(1), _secret(2)], _headers("m", ts, sig), body)


def test_empty_secret_list_rejects() -> None:
    """Accepting an unsigned request is HOOK_ALLOW_UNSIGNED's decision and
    main.py's to make; verify never says yes with no key."""
    body, ts = b"{}", _now()
    assert not verify([], _headers("m", ts, _sign(SECRET, "m", ts, body)), body)


def test_a_malformed_secret_does_not_shadow_a_later_good_one() -> None:
    body, ts = b"{}", _now()
    sig = _sign(SECRET, "m", ts, body)
    assert verify(["whsec_not*base64", _secret(1), SECRET], _headers("m", ts, sig), body)


def test_one_of_several_candidates_suffices() -> None:
    body, ts = b"{}", _now()
    sig = "v1,AAAA " + _sign(SECRET, "m", ts, body)
    assert verify([SECRET], _headers("m", ts, sig), body)


def test_header_lookup_is_case_insensitive() -> None:
    body, ts = b"{}", _now()
    sig = _sign(SECRET, "m", ts, body)
    headers = {"Webhook-Id": "m", "Webhook-Timestamp": ts, "Webhook-Signature": sig}
    assert verify([SECRET], headers, body)


def test_malformed_secret_is_skipped_not_crashed() -> None:
    body, ts = b"{}", _now()
    sig = _sign(SECRET, "m", ts, body)
    assert verify(["whsec_not*base64", SECRET], _headers("m", ts, sig), body)
