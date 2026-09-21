"""The models against the corpus — the test that stops them drifting.

Every committed fixture parses into the model the client hands the
readers, and nothing is silently dropped on the way: a row count that
fell, or a block that landed in the catch-all, means the wire moved and
a reader is reading less than it was.
"""

from __future__ import annotations

import pytest

from slashid_anthropic_forwarder.compliance.schema import (
    Activity,
    Chat,
    CursorPage,
    SessionListing,
    SessionTranscript,
    TextBlock,
    TokenPage,
    ToolResultBlock,
    ToolUseBlock,
    UnknownBlock,
)
from tests.compliance_fixtures import body

SESSIONS = [f"session_messages_{n}.json" for n in range(1, 7)]
CHATS = [f"chat_messages_{n}.json" for n in range(1, 4)]


def test_every_recorded_envelope_parses_whole() -> None:
    activities = CursorPage[Activity].model_validate(body("activities.json"))
    chats = CursorPage[Chat].model_validate(body("chats_list.json"))
    sessions = TokenPage[SessionListing].model_validate(body("sessions_list.json"))
    assert len(activities.data) == len(body("activities.json")["data"])
    assert len(chats.data) == len(body("chats_list.json")["data"])
    assert len(sessions.data) == len(body("sessions_list.json")["data"])
    # The three envelopes are three shapes, and each one's page token is
    # the half the client reads to decide whether to ask again.
    assert activities.has_more is False and activities.last_id
    assert sessions.next_page is None


@pytest.mark.parametrize("name", SESSIONS)
def test_a_session_transcript_keeps_its_rows_and_its_identity(name: str) -> None:
    raw = body(name)
    page = SessionTranscript.model_validate(raw)
    assert len(page.data) == len(raw["data"])
    # Identity is on the `session`, never on a message: a message carries
    # exactly type, id, role, created_at, provenance, model and content.
    assert page.session is not None and page.session.user is not None
    assert page.session.user.id
    for message, source in zip(page.data, raw["data"], strict=True):
        assert message.role == source["role"]
        assert len(message.content) == len(source["content"])
        assert message.at is not None


@pytest.mark.parametrize("name", CHATS)
def test_a_chat_transcript_keeps_its_turns_files_and_artifacts(name: str) -> None:
    raw = body(name)
    chat = Chat.model_validate(raw)
    # Under `chat_messages`, not `data`, and the model is on the object.
    assert len(chat.chat_messages) == len(raw["chat_messages"])
    assert chat.model == raw["model"] and chat.href
    for message, source in zip(chat.chat_messages, raw["chat_messages"], strict=True):
        assert len(message.content) == len(source["content"])
        # All three are nullable on the wire and lists here.
        assert len(message.files) == len(source["files"] or [])
        assert len(message.generated_files) == len(source["generated_files"] or [])
        assert len(message.artifacts) == len(source["artifacts"] or [])


def test_no_recorded_block_falls_into_the_catch_all() -> None:
    # text, tool_use and tool_result, and all three are modelled. A block
    # reaching UnknownBlock is tolerated by design — and if a *recorded*
    # one does, the wire moved under a reader that is now skipping it.
    blocks = [
        block
        for name in (*SESSIONS, *CHATS)
        for message in (body(name).get("data") or body(name)["chat_messages"])
        for block in message["content"]
    ]
    parsed = Chat.model_validate(
        {"chat_messages": [{"role": "assistant", "content": blocks}]}
    ).chat_messages[0]
    assert not [b for b in parsed.content if isinstance(b, UnknownBlock)]
    assert {type(b) for b in parsed.content} == {TextBlock, ToolUseBlock, ToolResultBlock}


def test_an_unparseable_row_is_dropped_and_the_page_survives() -> None:
    # The blast radius of an unmodelled shape is one row: a tick reads
    # three feeds and every transcript behind two of them, and raising
    # here would cost the records the other feeds already landed.
    page = CursorPage[Activity].model_validate(
        {"data": [{"type": "first"}, "not a row", {"type": "second"}], "has_more": False}
    )
    assert [row.type for row in page.data] == ["first", "second"]


def test_an_unknown_block_type_is_kept_as_unknown_never_rejected() -> None:
    chat = Chat.model_validate(
        {"chat_messages": [{"role": "assistant", "content": [{"type": "future_block"}]}]}
    )
    assert [type(b) for b in chat.chat_messages[0].content] == [UnknownBlock]
