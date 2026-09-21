"""The pending record: the partial event, the envelope, and the 1 MiB bound."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from slashid_ai_forwarder_core.events import (
    AIInvocationContent,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.hook.envelope import PARSED_AS
from slashid_anthropic_forwarder.record import (
    COMPLIANCE,
    ELISION,
    FILE_DIGESTS,
    HOOK,
    MAX_EVENT_BYTES,
    PARSED_AS_COMPLIANCE,
    PARSED_AS_HOOK,
    PARSED_AS_JOINED,
    Append,
    PendingRecord,
    event_fields,
    from_document,
    open_fields,
    parsed_as,
    to_event,
)

NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)


def json_size(body: dict[str, object]) -> int:
    return len(json.dumps(body, separators=(",", ":")).encode())


def an_event(**over: object) -> AIInvocationObservedV1:
    fields: dict[str, object] = {
        "request_id": "toolu_01Dqhr2d1w2UCUqbXhCSGutC",
        "timestamp": "2026-09-20T23:08:20+00:00",
        "identity_details": AnthropicIdentityDetails(user_id="user_01AbCdEfGhIjKlMnOpQrStUv"),
        "model": AIModel(id="claude-opus-5", provider="anthropic"),
        "parsed_as": PARSED_AS_HOOK,
        "conversation_id": "00000002-0000-4000-8000-000000000000",
    }
    return AIInvocationObservedV1.model_validate(fields | over)


def a_record(**over: object) -> PendingRecord:
    fields: dict[str, object] = {
        "address": "toolu_01Dqhr2d1w2UCUqbXhCSGutC",
        "event": event_fields(an_event())["event"],
        "deadline": NOW + timedelta(hours=1),
        "next_attempt_at": NOW + timedelta(hours=1),
        "contributed": [HOOK],
    }
    return PendingRecord(**(fields | over))  # ty: ignore[invalid-argument-type]


def test_the_event_is_stored_as_a_mapping_not_a_model() -> None:
    fields = event_fields(an_event(input=AIInvocationContent(redacted_text="hi")))
    assert isinstance(fields["event"], dict)
    assert fields["event"]["input"] == {"redacted_text": "hi"}
    # exclude_none: absent stays absent, so a merge cannot resurrect a null.
    assert "output" not in fields["event"]
    assert "elided" not in fields


def test_open_fields_carry_the_delivery_and_the_two_verdicts() -> None:
    fields = open_fields(
        an_event(),
        webhook_id="msg_011CfFXrZo19wubUcJjnSJa9",
        verdict="allow",
        composed_verdict="deny",
        contributed=HOOK,
    )
    assert fields["webhook_ids"] == Append(("msg_011CfFXrZo19wubUcJjnSJa9",))
    assert fields["contributed"] == Append((HOOK,))
    # Under shadow mode the two differ, and without the second the rollout
    # has nothing to show an operator.
    assert (fields["verdict"], fields["composed_verdict"]) == ("allow", "deny")


def test_an_unanswered_record_omits_the_verdict_keys_rather_than_nulling_them() -> None:
    """A reader-opened record has no verdict; merging a null would erase the
    one a later frame supplied."""
    fields = open_fields(an_event(), webhook_id="msg_x", contributed=COMPLIANCE)
    assert "verdict" not in fields and "composed_verdict" not in fields


def test_parsed_as_reads_joined_only_when_two_sources_contributed() -> None:
    assert parsed_as([HOOK]) == PARSED_AS_HOOK == PARSED_AS
    assert parsed_as([COMPLIANCE]) == "anthropic-compliance"
    assert parsed_as([HOOK, COMPLIANCE]) == "anthropic-joined"
    # "visited" is not "contributed": one source twice is still one source.
    assert parsed_as([HOOK, HOOK]) == PARSED_AS_HOOK


def test_to_event_validates_and_stamps_parsed_as() -> None:
    record = a_record(contributed=[HOOK, COMPLIANCE])
    event = to_event(record)
    assert isinstance(event, AIInvocationObservedV1)
    assert event.parsed_as == "anthropic-joined"
    assert event.request_id == "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def test_to_event_raises_on_a_record_that_never_became_an_event() -> None:
    """Validation at push is the point: a malformed record is a log line and
    a retry, not a silent drop and not a 500 on the request path."""
    with pytest.raises(ValueError):
        to_event(a_record(event={"request_id": "toolu_1"}))


def test_a_record_round_trips_through_a_document() -> None:
    record = a_record(webhook_ids=["msg_a", "msg_b"], awaiting=[FILE_DIGESTS], attempts=2)
    document = {
        "event": record.event,
        "webhook_ids": ["msg_a", "msg_b"],
        "deadline": record.deadline,
        "next_attempt_at": record.next_attempt_at,
        "awaiting": [FILE_DIGESTS],
        "contributed": [HOOK],
        "attempts": 2,
        "verdict": None,
        "composed_verdict": None,
        "claim_owner": None,
        "claim_expires_at": None,
        "tombstoned_at": None,
        "elided": False,
    }
    assert from_document(record.address, document) == record


def test_readiness_is_a_state_of_the_record() -> None:
    assert a_record().ready
    assert not a_record(awaiting=[FILE_DIGESTS]).ready
    assert not a_record(tombstoned_at=NOW).ready


@yaml_pytest(filename="test_elision.yaml")
def test_oversized_events_are_elided_in_size_order(
    input_chars: int,
    output_chars: int,
    file_chars: int,
    elided: bool,
    surviving: list[str],
) -> None:
    """The bound is enforced here, not in the adapter: dropping raw text is a
    decision about the event's content, and a write that fails is an event
    lost."""
    event = an_event(
        input=AIInvocationContent(
            redacted_text="i" * input_chars,
            content_hashes={"sha256": "a" * 64},
            byte_length=input_chars,
        ),
        output=AIInvocationContent(redacted_text="o" * output_chars, byte_length=output_chars),
        accessed_files=[{"name": "notes.txt", "redacted_content": "f" * file_chars}],
    )
    fields = event_fields(event)
    body = fields["event"]
    assert fields.get("elided", False) is elided
    assert json_size(body) <= MAX_EVENT_BYTES
    kept = [
        name
        for name, text in (
            ("input", body.get("input", {}).get("redacted_text")),
            ("output", body.get("output", {}).get("redacted_text")),
            ("file", (body.get("accessed_files") or [{}])[0].get("redacted_content")),
        )
        if text is not None and not text.startswith(ELISION[:20])
    ]
    assert kept == surviving
    # Whatever was dropped, the hashes that identify the content survive.
    assert body["input"]["content_hashes"] == {"sha256": "a" * 64}
    assert body["input"]["byte_length"] == input_chars


def test_a_record_with_no_raw_text_left_to_drop_still_fits() -> None:
    """The last resort, which no amount of raw text can reach: bulk that is
    not text at all. `_bound` promises it never fails, and a guarantee with
    no test is how a background write starts throwing at 3 a.m."""
    event = an_event(
        accessed_files=[
            {"name": f"/home/alice/proj/file_{i}.txt", "content_hashes": {"sha256": f"{i:064d}"}}
            for i in range(12_000)
        ]
    )
    fields = event_fields(event)
    assert fields["elided"] is True
    assert "accessed_files" not in fields["event"]
    assert json_size(fields["event"]) <= MAX_EVENT_BYTES


def _record_with(**over: Any) -> PendingRecord:
    base: dict[str, Any] = {
        "address": "toolu_01A",
        "event": {
            "request_id": "toolu_01A",
            "timestamp": "2026-09-20T23:08:20+00:00",
            "identity_details": {"kind": "anthropic", "user_id": "user_01A"},
            "model": {"id": "claude-opus-5"},
            "accessed_files": [
                {
                    "name": "src/a.py",
                    "content_hashes": {"sha256": "aa"},
                    "provenance": "tool_result",
                },
                {"name": None, "content_hashes": {"sha256": "bb"}, "provenance": "attachment"},
            ],
        },
        "deadline": NOW,
        "next_attempt_at": NOW,
        "contributed": [HOOK],
    }
    return PendingRecord(**(base | over))


def test_digests_replace_the_attachment_group_at_push() -> None:
    record = _record_with(
        file_digests=[
            {"name": "maria.txt", "content_hashes": {"md5": "cc"}, "provenance": "attachment"}
        ],
        contributed=[HOOK, COMPLIANCE],
    )
    event = to_event(record)
    assert [f.name for f in event.accessed_files or []] == ["src/a.py", "maria.txt"]
    # The frame's tool-result entry is untouched: it was hashed from an
    # untruncated transcript, which no reader can match.
    assert (event.accessed_files or [])[0].content_hashes == {"sha256": "aa"}
    assert event.parsed_as == PARSED_AS_JOINED


def test_an_empty_visit_keeps_what_the_frame_hashed() -> None:
    # A reader that found no listing still clears the expectation at the
    # store, but an empty replacement replaces nothing: an empty listing is
    # not evidence the round had no attachment, and a frame's
    # extracted-text digest is exact for plain text.
    event = to_event(_record_with(file_digests=[]))
    assert [f.name for f in event.accessed_files or []] == ["src/a.py", None]


def test_a_reader_only_record_is_labelled_compliance() -> None:
    event = to_event(_record_with(contributed=[COMPLIANCE]))
    assert event.parsed_as == PARSED_AS_COMPLIANCE


def test_from_document_carries_the_digests() -> None:
    record = from_document(
        "toolu_01A",
        {
            "event": {},
            "deadline": NOW,
            "next_attempt_at": NOW,
            "file_digests": [{"name": "maria.txt", "provenance": "attachment"}],
        },
    )
    assert record.file_digests == [{"name": "maria.txt", "provenance": "attachment"}]
