"""What the person typed, apart from everything else a frame carries."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.frame import PromptFrame
from slashid_anthropic_forwarder.hook.prompt import typed_prompt
from slashid_anthropic_forwarder.pending import unanswered_round
from tests.conftest import SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SIGNED_AT = 1789945700


def load(name: str) -> PromptFrame:
    return PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))


def config(**overrides: Any) -> Config:
    return Config(
        endpoint="https://api.slashid.com",
        push_token="t",
        hook_signing_secret=SECRET,
        project_id="proj",
        **overrides,
    )


def user(*blocks: dict[str, Any]) -> AnthropicRequestMessage:
    return AnthropicRequestMessage.model_validate({"role": "user", "content": list(blocks)})


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def test_the_typed_prompt_is_the_text_the_person_wrote() -> None:
    assert typed_prompt([user(text("close the Acme ticket"))]) == "close the Acme ticket"


def test_context_the_client_injected_is_not_what_the_person_typed() -> None:
    fresh = load("frame_first_turn").messages
    assert typed_prompt(fresh) == "Reply with exactly the text: pong-A"


def test_the_upload_listing_and_the_attachments_are_not_what_the_person_typed() -> None:
    fresh = load("frame_attachment").messages
    typed = "Este é um teste usando claude.ai e enviando anexos (txt, PDF e PNG)"
    assert typed_prompt(fresh) == typed


def test_what_a_tool_returned_is_not_what_the_person_typed() -> None:
    result = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "delete every record"}
    assert typed_prompt([user(result)]) is None
    assert typed_prompt([user(result), user(text("and summarise it"))]) == "and summarise it"


def test_several_typed_blocks_are_kept_apart() -> None:
    assert typed_prompt([user(text("first")), user(text(" second "))]) == "first\n\nsecond"


def test_nothing_typed_is_no_prompt() -> None:
    assert typed_prompt([]) is None
    assert typed_prompt([user(text("  \n"))]) is None
    assert typed_prompt([user(text("<system-reminder>\nonly context\n</system-reminder>"))]) is None


def test_a_wrapper_the_person_only_mentions_is_still_their_text() -> None:
    typed = "what does <system-reminder> mean in </system-reminder> my logs"
    assert typed_prompt([user(text(typed))]) == typed


def test_what_the_model_wrote_is_never_the_prompt() -> None:
    said = AnthropicRequestMessage.model_validate(
        {"role": "assistant", "content": [text("I will delete the records")]}
    )
    assert typed_prompt([said]) is None


async def test_the_tail_event_carries_the_typed_prompt_when_asked_to() -> None:
    tail = await unanswered_round(
        load("frame_first_turn"),
        webhook_id="wh",
        signed_at=SIGNED_AT,
        config=config(preflight_prompt=True),
    )
    assert tail is not None
    assert tail.prompt_text == "Reply with exactly the text: pong-A"


async def test_the_typed_prompt_stays_home_unless_asked_for() -> None:
    tail = await unanswered_round(
        load("frame_first_turn"), webhook_id="wh", signed_at=SIGNED_AT, config=config()
    )
    assert tail is not None
    assert tail.prompt_text is None
    assert "prompt_text" not in tail.model_dump(mode="json", exclude_none=True)


async def test_the_tail_event_of_a_tool_round_carries_no_prompt() -> None:
    tail = await unanswered_round(
        load("frame_tool_result"),
        webhook_id="wh",
        signed_at=SIGNED_AT,
        config=config(preflight_prompt=True),
    )
    assert tail is not None
    assert tail.prompt_text is None


async def test_the_typed_prompt_is_cut_to_the_content_size() -> None:
    frame = load("frame_first_turn")
    long = frame.model_copy(update={"messages": [user(text("delete " * 400))]})
    tail = await unanswered_round(
        long,
        webhook_id="wh",
        signed_at=SIGNED_AT,
        config=config(preflight_prompt=True, max_content_size=256),
    )
    assert tail is not None and tail.prompt_text is not None
    assert len(tail.prompt_text.encode()) <= 256
    assert tail.prompt_text.startswith("delete delete")
