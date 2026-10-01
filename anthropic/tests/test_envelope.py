"""Frame → the partial event a pending record is made of."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Literal

from pydantic import BaseModel
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIStopReason,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.envelope import (
    PARSED_AS,
    accessed_files_for,
    attachment_files,
    partial_event,
    signed_at_iso,
)
from slashid_anthropic_forwarder.hook.frame import PromptFrame
from slashid_anthropic_forwarder.pending import unanswered_round
from tests.conftest import SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# The attested webhook-timestamp of the captured deliveries.
SIGNED_AT = 1789945700
# The provisional key is computed by the store's addressing module, in a
# later chunk, and handed to the builder; nothing here derives it.
ADDRESS = "inv:0123456789abcdef0123456789abcdef"


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


class ExpectedFile(BaseModel):
    name: str | None
    sha256: str
    media_type: str | None = None
    byte_length: int | None = None
    provenance: Literal["tool_result", "attachment"] = "tool_result"


def check_files(files: list[AIAccessedFile] | None, expected: list[ExpectedFile]) -> None:
    got = files or []
    assert [(f.name, (f.content_hashes or {}).get("sha256")) for f in got] == [
        (e.name, e.sha256) for e in expected
    ]
    for g, e in zip(got, expected, strict=True):
        assert g.content_hashes is not None and set(g.content_hashes) == {"sha256", "sha1", "md5"}
        assert g.provenance == e.provenance
        if e.media_type is not None:
            assert g.media_type == e.media_type
        if e.byte_length is not None:
            assert g.byte_length == e.byte_length


@yaml_pytest(filename="test_accessed_files_for.yaml")
async def test_accessed_files_for(fixture: str, expected: list[ExpectedFile]) -> None:
    check_files(await accessed_files_for(load(fixture).messages, config=config()), expected)


async def test_a_nameless_attachment_takes_its_name_by_media_type() -> None:
    """The <uploaded_files> block lists pdf, jpeg, txt; the attachment blocks
    run txt, jpeg, pdf. Order cannot pair them, so the nameless PDF claims
    the listed name whose media type matches — and the image, carrying no
    text, produces no entry to name at all."""
    files = await accessed_files_for(load("frame_attachment").messages, config=config())
    assert [f.name for f in files] == ["maria.txt", "guiaSADT.pdf"]


async def test_attachment_text_rides_along_only_under_include_raw_content() -> None:
    messages = load("frame_attachment").messages
    files = attachment_files(messages, config=config(include_raw_content=True))
    # AIAccessedFile is a _WireModel (str_strip_whitespace=True), so the
    # stored text loses the trailing newline; the digest and byte_length
    # in the case table above are over the unstripped bytes.
    assert files[0].redacted_content == "Maria tinha um carneirinho"
    assert attachment_files(messages, config=config())[0].redacted_content is None


class ExpectedEvent(BaseModel):
    # Most runs in the table end in text; the three that end in a tool call
    # say so. A value outside AIStopReason fails the table at import time.
    stop_reason: AIStopReason = "end_turn"
    used_tool_ids: list[str] = []
    used_tool_errors: list[bool] = []
    accessed_files: list[ExpectedFile] = []
    tool_names: set[str] = set()
    servers: set[tuple[str, str]] = set()


def check_event(event: AIInvocationObservedV1, expected: ExpectedEvent) -> None:
    assert event.stop_reason == expected.stop_reason
    assert [u.tool_use_id for u in event.used_tools or []] == expected.used_tool_ids
    assert [u.is_error for u in event.used_tools or []] == expected.used_tool_errors
    check_files(event.accessed_files, expected.accessed_files)
    assert {t.name for t in event.available_tools or []} == expected.tool_names
    assert {(s.name, s.kind) for s in event.available_tool_servers or []} == expected.servers


def build(fixture: str, append: list[dict[str, Any]], insert_at: int | None) -> PromptFrame:
    """A fixture with extra messages spliced in: `insert_at: 0` gives it a
    previous round, `null` continues it with another one."""
    frame = load(fixture)
    extra = [AnthropicRequestMessage.model_validate(m) for m in append]
    messages = list(frame.messages)
    at = len(messages) if insert_at is None else insert_at
    return frame.model_copy(update={"messages": messages[:at] + extra + messages[at:]})


@yaml_pytest(filename="test_partial_event.yaml")
async def test_partial_event(
    fixture: str,
    insert_at: int | None,
    append: list[dict[str, Any]],
    expected: ExpectedEvent | None,
) -> None:
    frame = build(fixture, append, insert_at)
    event = await partial_event(frame, request_id=ADDRESS, signed_at=SIGNED_AT, config=config())
    if expected is None:
        assert event is None
        return
    assert event is not None
    check_event(event, expected)
    assert event.request_id == ADDRESS
    assert event.parsed_as == PARSED_AS
    assert event.conversation_id == frame.session_id
    assert event.input is not None and event.input.content_hashes is not None
    # Complete on arrival: the answer is the trailing run, which this frame
    # carries. Only attachment digests can still be outstanding.
    assert event.output is not None and event.output.content_hashes is not None


async def test_envelope_fields_come_from_the_frame() -> None:
    frame = load("frame_tool_result")
    event = await partial_event(frame, request_id=ADDRESS, signed_at=SIGNED_AT, config=config())
    assert event is not None
    assert event.timestamp == signed_at_iso(SIGNED_AT) == "2026-09-20T23:08:20+00:00"
    assert event.identity_details.model_dump(exclude_none=True) == {
        "kind": "anthropic",
        "user_id": frame.actor.id,
    }
    assert event.model.id == "claude-opus-5" and event.model.provider == "anthropic"
    assert event.model.raw_model_id == "claude-opus-5"
    assert event.user_agent == "claude-code"
    assert event.tokens.input == 0 and event.tokens.output == 0


async def test_input_ends_before_the_trailing_run() -> None:
    """The record names A(n-1), so its input stops at the round A(n-1)
    consumed: the trailing run and the fresh round are both outside it."""
    event = await partial_event(
        load("frame_subagent_child"),
        request_id=ADDRESS,
        signed_at=SIGNED_AT,
        config=config(include_raw_content=True, input_scope="session"),
    )
    assert event is not None and event.input is not None and event.output is not None
    body = event.input.redacted_text or ""
    assert "wc -l /home/alice/proj/a.txt" in body  # the transcript up to the round it consumed
    assert "toolu_01UbdhcQRwkxR8JFoAGZi2i9" not in body  # the trailing run itself
    assert "alpha line one" not in body  # the fresh round, which is the tail's
    # That run is not missing, it is the answer.
    assert "toolu_01UbdhcQRwkxR8JFoAGZi2i9" in (event.output.redacted_text or "")


async def test_round_scope_keeps_only_the_round_the_run_consumed() -> None:
    event = await partial_event(
        load("frame_subagent_child"),
        request_id=ADDRESS,
        signed_at=SIGNED_AT,
        config=config(include_raw_content=True),
    )
    assert event is not None and event.input is not None
    body = event.input.redacted_text or ""
    assert "3 /home/alice/proj/a.txt" in body  # the tool result it consumed
    assert "wc -l /home/alice/proj/a.txt" not in body  # earlier rounds are not repeated
    assert "toolu_01UbdhcQRwkxR8JFoAGZi2i9" not in body
    assert "alpha line one" not in body


async def test_a_tail_has_no_round_hash_and_lists_the_rounds_its_neighbour_does() -> None:
    """The trailing run is two messages here, which the record merges into one
    answer and the tail sees as two: both must hash the same round."""
    frame = load("frame_subagent_child")
    aside = AnthropicRequestMessage.model_validate(
        {"role": "assistant", "content": [{"type": "text", "text": "checking"}]}
    )
    split = frame.model_copy(update={"messages": [*frame.messages[:3], aside, *frame.messages[3:]]})
    event = await partial_event(split, request_id=ADDRESS, signed_at=SIGNED_AT, config=config())
    tail = await unanswered_round(split, webhook_id="wh", signed_at=SIGNED_AT, config=config())
    assert event is not None and tail is not None
    assert event.round_hash is not None
    assert tail.round_hash is None and tail.output is None
    assert tail.recent_round_hashes == event.recent_round_hashes
    assert event.recent_round_hashes is not None
    assert event.recent_round_hashes[0] == event.round_hash
    assert event.recent_round_hashes[-1] == "start"


async def test_the_fresh_round_cannot_change_the_record() -> None:
    """Two frames that agree up to the trailing run and differ only in what
    follows it build the same record, hash included."""
    frame = load("frame_subagent_child")
    other = frame.model_copy(
        update={
            "messages": [
                *frame.messages[:4],
                AnthropicRequestMessage.model_validate(
                    {"role": "user", "content": [{"type": "text", "text": "never mind"}]}
                ),
            ]
        }
    )
    a = await partial_event(frame, request_id=ADDRESS, signed_at=SIGNED_AT, config=config())
    b = await partial_event(other, request_id=ADDRESS, signed_at=SIGNED_AT, config=config())
    assert a is not None and b is not None and a.input is not None and b.input is not None
    assert a.input.content_hashes == b.input.content_hashes
    assert a.output == b.output and a.stop_reason == b.stop_reason
    assert a.used_tools == b.used_tools and a.accessed_files == b.accessed_files


async def test_a_multi_message_run_is_one_answer() -> None:
    """Consecutive assistant messages are one response delivered in pieces:
    their blocks concatenate in transcript order into a single output, and
    the stop reason follows the last block of the run, not the first."""
    frame = build(
        "frame_tool_result",
        [{"role": "assistant", "content": [{"type": "text", "text": "and then"}]}],
        2,
    )
    event = await partial_event(
        frame, request_id=ADDRESS, signed_at=SIGNED_AT, config=config(include_raw_content=True)
    )
    assert event is not None and event.output is not None
    answer = event.output.redacted_text or ""
    assert (
        answer.index("I'll read the file.")
        < answer.index("toolu_01Dqhr2d1w2UCUqbXhCSGutC")
        < answer.index("and then")
    )
    # The run ends in text although it contains a tool call.
    assert event.stop_reason == "end_turn"


async def test_null_model_becomes_unknown() -> None:
    frame = load("frame_tool_result").model_copy(update={"model": None})
    event = await partial_event(frame, request_id=ADDRESS, signed_at=SIGNED_AT, config=config())
    assert event is not None and event.model.id == "unknown" and event.model.raw_model_id is None


async def test_null_actor_id_drops_the_event() -> None:
    frame = load("frame_tool_result")
    frame.actor.id = None
    event = await partial_event(frame, request_id=ADDRESS, signed_at=SIGNED_AT, config=config())
    assert event is None
