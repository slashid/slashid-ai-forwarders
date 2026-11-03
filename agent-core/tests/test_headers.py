from slashid_agentcore_forwarder.headers import Baggage, parse_baggage, parse_headers


def test_headers_are_case_insensitive_and_read_only_what_we_use() -> None:
    h = parse_headers({"USER-AGENT": "ua", "Baggage": "k=v", "Authorization": "Bearer secret"})
    assert (h.user_agent, h.baggage) == ("ua", "k=v")
    assert "secret" not in repr(h)
    assert parse_headers({"user-agent": 3}).user_agent is None
    assert parse_headers(None).baggage is None


def test_a_long_user_agent_is_dropped() -> None:
    assert parse_headers({"user-agent": "u" * 257}).user_agent is None
    assert parse_headers({"user-agent": "u" * 256}).user_agent == "u" * 256


def test_baggage_drops_properties_decodes_and_bounds_values() -> None:
    b = parse_baggage("Self=x;prop=1,harness.id=h%2Dabc,session.id=" + "s" * 300)
    assert b.harness_id == "h-abc"
    assert b.session_id is None  # over 256 characters
    assert parse_baggage(None) == Baggage()
    assert parse_baggage("garbage") == Baggage()


def test_merged_falls_back_field_by_field() -> None:
    first = Baggage(harness_id="h", session_id=None)
    second = Baggage(harness_id="other", session_id="s")
    assert first.merged(second) == Baggage(harness_id="h", session_id="s")
