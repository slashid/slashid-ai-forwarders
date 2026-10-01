"""Tests for the round projection, split and hash chain."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

import pytest

from slashid_ai_forwarder_core import rounds
from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedMessage,
)
from slashid_ai_forwarder_core.rounds import (
    START,
    TRUNCATED,
    Round,
    project,
    round_links,
)


def _text(role: Literal["system", "user", "assistant"], text: str) -> NormalizedMessage:
    return NormalizedMessage(role=role, content=[NormalizedContent(kind="text", text=text)])


def _user(text: str) -> NormalizedMessage:
    return _text("user", text)


def _assistant(text: str) -> NormalizedMessage:
    return _text("assistant", text)


def _dump(messages: list[NormalizedMessage]) -> list[dict]:
    return [m.model_dump(mode="json", exclude_none=True) for m in project(messages)]


def test_project_drops_system_messages() -> None:
    assert _dump([_text("system", "be nice"), _user("hi")]) == [
        {"role": "user", "content": [{"kind": "text", "text": "hi"}]}
    ]


def test_project_drops_reasoning_and_the_message_it_empties() -> None:
    thinking = NormalizedMessage(
        role="assistant", content=[NormalizedContent(kind="reasoning", text="hmm")]
    )
    assert project([thinking]) == []
    mixed = NormalizedMessage(
        role="assistant",
        content=[
            NormalizedContent(kind="reasoning", text="hmm"),
            NormalizedContent(kind="text", text="ok"),
        ],
    )
    assert _dump([mixed]) == [{"role": "assistant", "content": [{"kind": "text", "text": "ok"}]}]


def test_project_keeps_tool_ids_and_payloads_only() -> None:
    call = NormalizedMessage(
        role="assistant",
        content=[
            NormalizedContent(
                kind="tool_use",
                tool_use_id="t1",
                tool_name="Read",
                tool_input={"p": "a"},
                tool_executor="client",
            )
        ],
    )
    result = NormalizedMessage(
        role="user",
        content=[
            NormalizedContent(
                kind="tool_result",
                tool_use_id="t1",
                tool_output="x",
                tool_is_error=True,
                byte_length=9,
            )
        ],
    )
    assert _dump([call, result]) == [
        {
            "role": "assistant",
            "content": [
                {
                    "kind": "tool_use",
                    "tool_use_id": "t1",
                    "tool_name": "Read",
                    "tool_input": {"p": "a"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "kind": "tool_result",
                    "tool_use_id": "t1",
                    "tool_output": "x",
                    "tool_is_error": True,
                }
            ],
        },
    ]


def test_project_coalesces_attachments_to_a_bare_marker() -> None:
    blocks = [
        NormalizedContent(kind=kind, media_type=None, byte_length=n)
        for kind, n in (("image", 1), ("audio", 2), ("document", 3))
    ]
    out = _dump([NormalizedMessage(role="user", content=blocks)])
    assert out == [{"role": "user", "content": [{"kind": "attachment"}] * 3}]


def test_round_hash_is_stable_and_ignores_system_and_reasoning() -> None:
    plain = Round([_user("a")], [_assistant("b")]).digest()
    noisy = Round(
        [_text("system", "s"), _user("a")],
        [
            NormalizedMessage(
                role="assistant",
                content=[
                    NormalizedContent(kind="reasoning", text="r"),
                    NormalizedContent(kind="text", text="b"),
                ],
            )
        ],
    ).digest()
    assert plain is not None
    assert plain == noisy
    assert plain != Round([_user("a")], [_assistant("c")]).digest()


def test_round_hash_needs_an_answer() -> None:
    assert Round([_user("a")], []).digest() is None
    assert Round([_user("a")], [NormalizedMessage(role="assistant", content=[])]).digest() is None


def _history(rounds: int) -> list[NormalizedMessage]:
    out: list[NormalizedMessage] = []
    for i in range(rounds):
        out += [_user(f"u{i}"), _assistant(f"a{i}")]
    return out


def test_first_event_lists_its_own_round_and_the_guard() -> None:
    own, recent = round_links([_user("u0"), _assistant("a0")], depth=10)
    assert own == Round([_user("u0")], [_assistant("a0")]).digest()
    assert recent == [own, START]


def test_links_are_newest_first_and_end_at_start_when_short() -> None:
    own, recent = round_links([*_history(2), _user("u2"), _assistant("a2")], depth=10)
    assert recent[0] == own
    assert recent[1:] == [
        Round([_user("u1")], [_assistant("a1")]).digest(),
        Round([_user("u0")], [_assistant("a0")]).digest(),
        START,
    ]


def test_start_marks_exactly_the_events_that_reach_round_one_and_the_rest_are_truncated() -> None:
    _, tenth = round_links([*_history(9), _user("u9"), _assistant("a9")], depth=10)
    _, eleventh = round_links([*_history(10), _user("u10"), _assistant("a10")], depth=10)
    assert len(tenth) == 11 and tenth[-1] == START
    assert len(eleventh) == 11 and eleventh[-1] == TRUNCATED and START not in eleventh


def test_consecutive_events_share_all_but_one_hash() -> None:
    _, a = round_links([*_history(12), _user("u12"), _assistant("a12")], depth=10)
    _, b = round_links([*_history(13), _user("u13"), _assistant("a13")], depth=10)
    assert (set(a) & set(b)) - {TRUNCATED} == set(a[:9])


def test_no_answer_lists_the_complete_rounds_and_no_own_hash() -> None:
    own, recent = round_links([*_history(2), _user("u2")], depth=10)
    assert own is None
    assert recent == [
        Round([_user("u1")], [_assistant("a1")]).digest(),
        Round([_user("u0")], [_assistant("a0")]).digest(),
        START,
    ]


def test_an_empty_answer_counts_as_no_answer() -> None:
    empty = NormalizedMessage(role="assistant", content=[])
    assert round_links([_user("u0"), empty], depth=3) == (None, [])


def test_no_complete_round_and_no_answer_has_no_links() -> None:
    assert round_links([_text("system", "s"), _user("a")], depth=10) == (None, [])


def test_project_merges_adjacent_messages_of_one_role() -> None:
    merged = _dump([_user("a"), _user("b"), _assistant("c"), _assistant("d")])
    assert merged == [
        {"role": "user", "content": [{"kind": "text", "text": "a"}, {"kind": "text", "text": "b"}]},
        {
            "role": "assistant",
            "content": [{"kind": "text", "text": "c"}, {"kind": "text", "text": "d"}],
        },
    ]


def test_a_response_run_hashes_alike_merged_or_split() -> None:
    one = NormalizedMessage(
        role="assistant",
        content=[
            NormalizedContent(kind="text", text="x"),
            NormalizedContent(kind="text", text="y"),
        ],
    )
    assert (
        Round([_user("a")], [one]).digest()
        == Round([_user("a")], [_assistant("x"), _assistant("y")]).digest()
    )


def test_a_response_and_its_replay_in_the_next_request_link_alike() -> None:
    merged = NormalizedMessage(
        role="assistant",
        content=[
            NormalizedContent(kind="text", text="x"),
            NormalizedContent(kind="text", text="y"),
        ],
    )
    own, _ = round_links([_user("a"), merged], depth=10)
    replay = [_user("a"), _assistant("x"), _assistant("y"), _user("b")]
    _, recent = round_links([*replay, _assistant("z")], depth=10)
    assert own in recent


def test_no_transcript_and_no_answer_has_no_links() -> None:
    assert round_links([], depth=10) == (None, [])


def test_a_round_whose_answer_projects_empty_is_skipped_and_not_counted() -> None:
    thinking = NormalizedMessage(
        role="assistant", content=[NormalizedContent(kind="reasoning", text="hm")]
    )
    _, recent = round_links([_user("a"), thinking, _user("b"), _assistant("c")], depth=2)
    assert len(recent) == 2 and recent[-1] == START


def test_events_stitch_after_up_to_n_minus_m_are_lost() -> None:
    n, m = 10, 4

    def links(k: int) -> list[str]:
        history = [x for i in range(k) for x in (_user(f"u{i}"), _assistant(f"a{i}"))]
        return round_links([*history, _user(f"u{k}"), _assistant(f"a{k}")], depth=n)[1]

    def stitches(a: list[str], b: list[str]) -> bool:
        real_a = [h for h in a if h not in (START, TRUNCATED)]
        real_b = [h for h in b if h not in (START, TRUNCATED)]
        shorter = a if len(real_a) <= len(real_b) else b
        need = min(m, len(real_a), len(real_b)) if START in shorter else m
        return len(set(real_a) & set(real_b)) >= need

    for k in range(0, 40):
        for lost in range(0, n - m):
            assert stitches(links(k), links(k + 1 + lost)), (k, lost)
    assert not stitches(links(0), links(n))  # far past the window: nothing shared


def test_a_run_of_assistant_messages_is_one_round_in_the_list() -> None:
    merged = round_links(
        [_user("a"), _assistant("b"), _assistant("c"), _user("d"), _assistant("e")],
        depth=10,
    )
    split = round_links([_user("a"), _assistant("b"), _user("d"), _assistant("e")], depth=10)
    assert len(merged[1]) == len(split[1])


def test_depth_bounds_the_list() -> None:
    _, recent = round_links([*_history(30), _user("u30"), _assistant("a30")], depth=4)
    assert len(recent) == 5 and recent[-1] == TRUNCATED


def test_a_history_ending_on_an_assistant_message_merges_with_the_answer() -> None:
    own, recent = round_links([_user("a"), _assistant("b"), _assistant("c")], depth=10)
    assert recent == [own, START]
    assert own == Round([_user("a")], [_assistant("b"), _assistant("c")]).digest()


def test_a_response_run_split_across_history_and_answer_hashes_like_one_message() -> None:
    merged = NormalizedMessage(
        role="assistant",
        content=[
            NormalizedContent(kind="text", text="b"),
            NormalizedContent(kind="text", text="c"),
        ],
    )
    split, _ = round_links([_user("a"), _assistant("b"), _assistant("c")], depth=10)
    whole, _ = round_links([_user("a"), merged], depth=10)
    assert split == whole


def test_the_work_is_bounded_by_depth_not_by_history(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = rounds.project

    def counting(messages: Sequence[NormalizedMessage]):
        nonlocal calls
        calls += 1
        return real(messages)

    history = [x for i in range(1000) for x in (_user(f"u{i}"), _assistant(f"a{i}"))]
    monkeypatch.setattr(rounds, "project", counting)
    rounds.round_links([*history, _user("u1000"), _assistant("a1000")], depth=10)
    assert calls < 60
