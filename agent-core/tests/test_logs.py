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


def test_every_line_has_exactly_its_keys(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="slashid_agentcore_forwarder")
    logs.fail_mode(surface="harness", check="call", reason="timeout", mode="allow")
    logs.pass_through(surface="gateway", reason="unknown_payload", keys=["a"])
    logs.shadow(surface="gateway", check="result", verdict="deny", answered="allow")
    logs.truncated_input(surface="harness", tool="Bash", call_id="c1")
    logs.baggage_unverified("mismatch")
    logs.gateway_role_unavailable()
    logs.role_lookup_failed("harness", "timeout")
    logs.cache_miss(tool="Bash", call_id="c1", known=True)
    logs.unreadable(surface="gateway", check="call", reason="bad_envelope")
    logs.missing_request_id()
    assert [(r["event"], sorted(r)) for r in rows(caplog)] == [
        ("fail_mode", ["check", "event", "mode", "reason", "surface"]),
        ("pass_through", ["event", "keys", "reason", "surface"]),
        ("shadow", ["answered", "check", "event", "surface", "verdict"]),
        ("truncated_input", ["call_id", "event", "surface", "tool"]),
        ("baggage_unverified", ["event", "reason"]),
        ("gateway_role_unavailable", ["event"]),
        ("role_lookup_failed", ["error", "event", "kind"]),
        ("cache_miss", ["call_id", "event", "known", "tool"]),
        ("unreadable", ["check", "event", "reason", "surface"]),
        ("missing_request_id", ["event"]),
    ]


def test_levels_and_values_of_the_other_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="slashid_agentcore_forwarder")
    logs.shadow(surface="gateway", check="result", verdict="deny", answered="allow")
    logs.cache_miss(tool="Bash", call_id="c1", known=True)
    logs.cache_miss(tool="Bash", call_id="c2", known=False)
    assert [r.levelno for r in caplog.records][1:] == [logging.INFO, logging.DEBUG]
    assert [r.get("known") for r in rows(caplog)] == [None, True, False]
    assert rows(caplog)[0] == {
        "event": "shadow",
        "surface": "gateway",
        "check": "result",
        "verdict": "deny",
        "answered": "allow",
    }


def test_an_unknown_cache_miss_is_silent_at_info(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="slashid_agentcore_forwarder")
    logs.cache_miss(tool="Bash", call_id="c2", known=False)
    assert caplog.records == []


def test_caller_chosen_text_is_bounded(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="slashid_agentcore_forwarder")
    logs.pass_through(
        surface="harness", reason="unknown_payload", keys=[f"{i}" + "k" * 100 for i in range(30)]
    )
    logs.truncated_input(surface="harness", tool="t" * 300, call_id="c" * 300)
    logs.cache_miss(tool="t" * 300, call_id="c" * 300, known=True)
    keys = rows(caplog)[0]["keys"]
    assert isinstance(keys, list) and len(keys) == 20
    assert [len(str(k)) for k in keys] == [64] * 20 and str(keys[1]).startswith("1k")
    for row in rows(caplog)[1:]:
        assert (row["tool"], row["call_id"]) == ("t" * 128, "c" * 128)
