import json

import pytest
from pydantic import JsonValue

from slashid_agentcore_forwarder.deny import (
    deny_text,
    derive_key,
    is_own_deny,
    single_text,
    with_reference,
)

KEY = derive_key("push-token")


def test_a_reference_verifies_and_covers_the_text() -> None:
    text = with_reference("Blocked.", KEY)
    assert text.startswith("Blocked. (ref ") and is_own_deny(text, KEY)
    assert not is_own_deny(text.replace("Blocked", "Allowed"), KEY)


def test_another_key_or_trailing_text_does_not_verify() -> None:
    text = with_reference("Blocked.", KEY)
    assert not is_own_deny(text, derive_key("other"))
    assert not is_own_deny(text + " ", KEY)
    assert not is_own_deny(text + "\n", KEY)
    assert not is_own_deny("Blocked.", KEY)


def test_reasons_are_joined_prefixed_and_capped_before_the_reference() -> None:
    text = deny_text(["a.", "b."], KEY, prefix="Content withheld: ")
    assert text.startswith("Content withheld: a. b. (ref ") and is_own_deny(text, KEY)
    long = deny_text(["é" * 900], KEY, prefix="Content withheld: ")
    body = long.rsplit(" (ref ", 1)[0]
    assert len(body) == 500 and is_own_deny(long, KEY)


@pytest.mark.parametrize(
    ("content", "structured", "want"),
    [
        ([{"type": "text", "text": "x"}], None, "x"),
        ([{"text": "x"}], None, "x"),
        ([{"text": "x"}, {"text": "y"}], None, None),
        ([{"text": "x"}], {"a": 1}, None),
        ([{"type": "image", "data": "d"}], None, None),
        ([{"type": "image", "text": "x"}], None, None),
        ([], None, None),
        ("x", None, None),
    ],
)
def test_only_a_lone_text_block_can_be_our_deny(
    content: JsonValue, structured: JsonValue, want: str | None
) -> None:
    assert single_text(content, structured) == want


def test_a_lone_surrogate_neither_raises_nor_verifies() -> None:
    lone = json.loads('"\\ud800 (ref abcdef012345)"')
    assert not is_own_deny(lone, KEY)
    assert not is_own_deny(json.loads('"\\ud800"'), KEY)
    text = with_reference(lone, KEY)
    assert is_own_deny(text, KEY)
    assert not is_own_deny(text.replace("\ud800", "\ud801"), KEY)


def test_no_usable_reasons_fall_back_to_a_default() -> None:
    for reasons in ([], ["", "  "]):
        text = deny_text(reasons, KEY)
        assert text.startswith("Blocked by your organization's policy. (ref ")
        assert is_own_deny(text, KEY)
    prefixed = deny_text([], KEY, prefix="Content withheld: ")
    assert prefixed.startswith("Content withheld: Blocked by your organization's policy. (ref ")
