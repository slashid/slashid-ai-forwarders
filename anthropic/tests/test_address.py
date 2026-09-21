"""The three addresses a pending record can be filed under."""

from __future__ import annotations

import json
import pathlib

from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.address import hook_address, joinable_address
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
