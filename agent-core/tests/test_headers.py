from slashid_agentcore_forwarder.headers import Baggage, parse_baggage, parse_headers


def test_headers_are_case_insensitive_and_read_only_what_we_use() -> None:
    h = parse_headers({"USER-AGENT": "ua", "Baggage": "k=v", "Authorization": "Bearer secret"})
    assert (h.user_agent, h.baggage) == ("ua", "k=v")
    assert "secret" not in repr(h)
    assert parse_headers({"user-agent": 3}).user_agent is None
    mixed = parse_headers({"user-agent": 3, "baggage": "session.id=s"})
    assert (mixed.user_agent, mixed.baggage) == (None, "session.id=s")
    assert parse_headers({"user-agent": "ua", "baggage": 3}).user_agent == "ua"
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


def test_a_property_on_the_wanted_member_is_dropped() -> None:
    assert parse_baggage("harness.id=h;p=1").harness_id == "h"


def test_a_value_with_a_control_character_or_replacement_char_is_dropped() -> None:
    assert parse_baggage("harness.id=a%0Ab,session.id=ok").harness_id is None
    assert parse_baggage("harness.id=a%00b,session.id=ok").session_id == "ok"
    assert parse_baggage("harness.id=a%EF%BF%BDb").harness_id is None  # U+FFFD
    assert parse_baggage("harness.id=a%FFb").harness_id is None  # invalid UTF-8
    assert parse_baggage("harness.id=a%E2%80%A8b").harness_id is None  # line separator


def test_the_first_occurrence_of_a_key_wins() -> None:
    b = parse_baggage("harness.id=first,harness.id=second,session.id=s1,session.id=s2")
    assert (b.harness_id, b.session_id) == ("first", "s1")
    # a dropped first occurrence does not let a later one in
    assert parse_baggage("harness.id=bad%0A,harness.id=second").harness_id is None


def test_merged_falls_back_field_by_field() -> None:
    first = Baggage(harness_id="h", session_id=None)
    second = Baggage(harness_id="other", session_id="s")
    assert first.merged(second) == Baggage(harness_id="h", session_id="s")
