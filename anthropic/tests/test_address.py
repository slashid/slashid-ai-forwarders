"""The four addresses a pending record can be filed under."""

from __future__ import annotations

import json
import pathlib
import unicodedata

from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.address import (
    _canonical_bytes,
    deny_address,
    hook_address,
    joinable_address,
    tail_address,
)
from slashid_anthropic_forwarder.hook.frame import PromptFrame, split_transcript

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> PromptFrame:
    return PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))


@yaml_pytest(filename="test_joinable_address.yaml")
def test_joinable_address_of_a_frames_trailing_run(fixture: str, expected: str | None) -> None:
    assert joinable_address(split_transcript(load(fixture)).assistant_run) == expected


def test_an_empty_run_has_no_address() -> None:
    """A first turn: nothing has been answered yet, so there is nothing to address."""
    assert joinable_address([]) is None


def test_a_run_split_across_two_messages_takes_the_first_id() -> None:
    """Consecutive assistant messages are one response however many messages
    they arrived as, so the address is the first tool call in the run."""
    run = [
        AnthropicRequestMessage.model_validate(
            {"role": "assistant", "content": [{"type": "text", "text": "on it"}]}
        ),
        AnthropicRequestMessage.model_validate(
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_first", "tool_name": "Read", "input": {}},
                    {"type": "tool_use", "id": "toolu_second", "tool_name": "Bash", "input": {}},
                ],
            }
        ),
    ]
    assert joinable_address(run) == "toolu_first"


def test_a_reader_shaped_run_yields_the_identical_address() -> None:
    """The load-bearing property. The compliance transcript spells the tool
    name `name` and carries fields no frame has; the `toolu_` id is the one
    thing both surfaces carry verbatim, and it is all this function reads."""
    frame_run = split_transcript(load("frame_tool_result")).assistant_run
    reader_run = [
        AnthropicRequestMessage.model_validate(
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01Dqhr2d1w2UCUqbXhCSGutC",
                        "name": "Read",
                        "input": {"file_path": "/home/alice/proj/notes.txt"},
                        "integration_name": "File Creation",
                    },
                ],
            }
        )
    ]
    assert joinable_address(reader_run) == joinable_address(frame_run)


def test_hook_address_prefixes_the_delivery_id() -> None:
    """Unjoinable by construction: no reader can compute a delivery id, which
    is exactly why nothing but the hook may emit under this key."""
    assert hook_address("msg_011CfFXrZo19wubUcJjnSJa9") == "hook:msg_011CfFXrZo19wubUcJjnSJa9"


def message(role: str, *blocks: dict) -> AnthropicRequestMessage:
    return AnthropicRequestMessage.model_validate({"role": role, "content": list(blocks)})


TINY = [
    message("user", {"type": "text", "text": "hello"}),
    message(
        "assistant",
        {"type": "thinking", "thinking": "ignored"},
        {"type": "tool_use", "id": "toolu_1", "tool_name": "Read", "input": {"p": "/tmp/x"}},
    ),
]


def test_the_canonical_encoding_is_exactly_these_bytes() -> None:
    """The private helper is asserted directly on purpose: this byte string
    IS the contract between the frame that writes a tail record and the
    successor frame that discards it. A change here that keeps the digest
    stable still breaks nothing loudly — it just leaks a duplicate event
    per session — so the bytes are pinned rather than the behaviour."""
    assert _canonical_bytes(TINY, "sess-1") == (
        b"\x00\x00\x00\x06tail/1"
        b"\x00\x00\x00\x06sess-1"
        b"\x00\x00\x00\x012"
        b"\x00\x00\x00\x04user\x00\x00\x00\x011"
        b"\x00\x00\x00\x01t\x00\x00\x00\x05hello"
        b"\x00\x00\x00\tassistant\x00\x00\x00\x011"
        b"\x00\x00\x00\x01u\x00\x00\x00\x07toolu_1\x00\x00\x00\x04Read"
    )


def test_the_tail_address_is_that_digest() -> None:
    assert tail_address(TINY, "sess-1") == (
        "tail:ba9afe1a9f8d86aa871f1a8a9d8888d8405a1eceeaa0d90650f56f5db4163231"
    )
    # A null session_id is legal on the wire and contributes an empty field
    # rather than being skipped, so the two cannot collide.
    assert tail_address(TINY, None) == (
        "tail:b99bf0681045d16e6258b2b985bedf055715b45dd5966822526bb78058896a34"
    )


def test_unmodelled_blocks_contribute_nothing() -> None:
    """A block type invented next quarter must not move an existing key."""
    plus_unknown = [
        TINY[0],
        message(
            "assistant",
            {"type": "thinking", "thinking": "ignored"},
            {"type": "tool_use", "id": "toolu_1", "tool_name": "Read", "input": {"p": "/tmp/x"}},
            {"type": "sparkle", "glitter": 1},
        ),
    ]
    assert tail_address(plus_unknown, "sess-1") == tail_address(TINY, "sess-1")


def test_text_past_the_prefix_does_not_move_the_key() -> None:
    a = [message("user", {"type": "text", "text": "x" * 300 + "A"})]
    b = [message("user", {"type": "text", "text": "x" * 300 + "B"})]
    assert tail_address(a, "s") == tail_address(b, "s")


def test_text_inside_the_prefix_does() -> None:
    a = [message("user", {"type": "text", "text": "read a.txt"})]
    b = [message("user", {"type": "text", "text": "read b.txt"})]
    assert tail_address(a, "s") != tail_address(b, "s")


def test_the_same_text_in_two_normal_forms_is_one_key() -> None:
    """The frame and any reconstruction of it must agree even if a client
    re-encodes; NFC is applied before the prefix is taken."""
    nfc = [message("user", {"type": "text", "text": unicodedata.normalize("NFC", "café")})]
    nfd = [message("user", {"type": "text", "text": unicodedata.normalize("NFD", "café")})]
    assert tail_address(nfc, "s") == tail_address(nfd, "s")


@yaml_pytest(filename="test_tail_reconstruction.yaml")
def test_a_successor_frame_reconstructs_its_predecessors_tail_key(
    fixture: str, predecessor_messages: int
) -> None:
    """Frame N writes tail_address(its whole transcript). Frame N+1 drops its
    own trailing assistant run and the round after it — which is exactly
    `split_transcript(...).before` — and lands on the same key, with no
    per-session pointer to collide across the sub-conversations that share a
    session_id."""
    successor = load(fixture)
    predecessor = successor.model_copy(
        update={"messages": successor.messages[:predecessor_messages]}
    )
    written = tail_address(predecessor.messages, predecessor.session_id)
    reconstructed = tail_address(split_transcript(successor).before, successor.session_id)
    assert reconstructed == written
    # And it is not the successor's own tail key, which is still outstanding.
    assert tail_address(successor.messages, successor.session_id) != written


def test_a_denial_is_keyed_on_its_delivery() -> None:
    assert deny_address("msg_1") == "deny:msg_1"


def test_a_denial_and_an_unjoinable_run_on_one_delivery_do_not_collide() -> None:
    """Both can arrive on the same frame, and merging them would leave one
    record holding the other's event."""
    assert deny_address("msg_1") != hook_address("msg_1")
