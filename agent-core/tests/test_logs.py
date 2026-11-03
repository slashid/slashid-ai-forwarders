import json
import logging

import pytest

from slashid_agentcore_forwarder import logs


def rows(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    return [json.loads(r.message) for r in caplog.records]


def test_lines_are_json_with_known_keys(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="slashid_agentcore_forwarder")
    logs.fail_mode(surface="harness", check="call", reason="timeout", mode="allow")
    logs.pass_through(surface="gateway", reason="unknown_payload", keys=["a", "b"])
    first, second = rows(caplog)
    assert first == {
        "event": "fail_mode",
        "surface": "harness",
        "check": "call",
        "reason": "timeout",
        "mode": "allow",
    }
    assert second == {
        "event": "pass_through",
        "surface": "gateway",
        "reason": "unknown_payload",
        "keys": ["a", "b"],
    }


def test_a_none_field_is_omitted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="slashid_agentcore_forwarder")
    logs.pass_through(surface="harness", reason="unknown_payload")
    assert rows(caplog) == [
        {"event": "pass_through", "surface": "harness", "reason": "unknown_payload"}
    ]


def test_the_other_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="slashid_agentcore_forwarder")
    logs.shadow(surface="gateway", check="result", verdict="deny", answered="allow")
    logs.truncated_input(surface="harness", tool="Bash", call_id="c1")
    logs.baggage_unverified("role_mismatch")
    logs.gateway_role_unavailable()
    logs.cache_miss(tool="Bash", call_id="c1", known=True)
    logs.cache_miss(tool="Bash", call_id="c2", known=False)
    logs.unreadable(surface="gateway", check="call", reason="bad_json")
    logs.missing_request_id()
    assert [(r["event"], r.get("known")) for r in rows(caplog)] == [
        ("shadow", None),
        ("truncated_input", None),
        ("baggage_unverified", None),
        ("gateway_role_unavailable", None),
        ("cache_miss", True),
        ("cache_miss", False),
        ("unreadable", None),
        ("missing_request_id", None),
    ]
    assert [r.levelno for r in caplog.records][4:6] == [logging.INFO, logging.DEBUG]
    assert rows(caplog)[0] == {
        "event": "shadow",
        "surface": "gateway",
        "check": "result",
        "verdict": "deny",
        "answered": "allow",
    }
