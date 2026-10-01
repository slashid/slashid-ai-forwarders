# Input scope and round hashes Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `AIInvocationObservedV1.input` hashes only the round's messages (default), and every event carries `round_hash` and `recent_round_hashes` for stitching.

**Architecture:** One new shared module, `rounds.py`, owns the projection, the round split and the hash chain. `build_event_from_normalized` is the only call site that changes behaviour: it slices and serializes `input` as a message list and attaches the two new wire fields. Every forwarder (Bedrock, Vertex, Anthropic hook and reader) already goes through that builder, so none needs code beyond tests and README rows.

**Tech Stack:** Python 3.13, pydantic v2, pydantic-settings, pytest (asyncio auto), ruff, ty, uv workspace.

**Spec:** `docs/superpowers/specs/2026-09-30-input-scope-and-round-hashes-design.md`. Read it first; this plan does not restate its reasoning.

**Implementation note:** review of the first implementation reworked `round_links` to walk the history backwards, so only the last N rounds are projected, and to merge an answer into a history that ends on an assistant message. `shared/src/slashid_ai_forwarder_core/rounds.py` is authoritative where it differs from the code below.

**Conventions to follow** (repo owner's rules): parse boundary data into pydantic models, no `dict.get()` / `["key"]` / `Mapping[str, Any]` on them; keep comments and docstrings terse, no bullet lists or narrated reasoning in docstrings; never merge the PR.

**Run everything from the worktree root** `~/.config/superpowers/worktrees/slashid-ai-forwarder/docs-input-scope-round-hashes`. Per-package checks: `cd <pkg> && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q`. Baseline: `shared` passes 411 tests.

## File structure

| File | Change |
| --- | --- |
| `shared/src/slashid_ai_forwarder_core/rounds.py` | new: `project`, `completed_rounds`, `round_hash`, `round_links`, `CONVERSATION_START` |
| `shared/src/slashid_ai_forwarder_core/config_base.py` | add `input_scope`, `round_link_depth` |
| `shared/src/slashid_ai_forwarder_core/events.py` | two wire fields; builder slices `input` and fills the fields |
| `shared/tests/test_rounds.py` | new |
| `shared/tests/test_config_base.py` | new |
| `shared/tests/test_events.py` | new builder tests; update input-hash expectations |
| `anthropic/tests/test_pending.py` | tail and denial records carry no `round_hash` |
| `anthropic/README.md`, `vertex/README.md` | two env rows each |

Not in scope: exposing the two settings in Terraform/CloudFormation (they default sensibly), and a "history is windowed" flag for the guard (no current source is windowed; Codex adds it when it lands). `output` keeps its current serialization.

## Chunk 1: shared library

### Task 1: Rounds module

**Files:**
- Create: `shared/src/slashid_ai_forwarder_core/rounds.py`
- Test: `shared/tests/test_rounds.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Tests for the round projection, split and hash chain."""

from __future__ import annotations

from typing import Literal

from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedMessage,
)
from slashid_ai_forwarder_core.rounds import (
    CONVERSATION_START,
    completed_rounds,
    project,
    round_hash,
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
        content=[NormalizedContent(kind="reasoning", text="hmm"), NormalizedContent(kind="text", text="ok")],
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
                {"kind": "tool_use", "tool_use_id": "t1", "tool_name": "Read", "tool_input": {"p": "a"}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"kind": "tool_result", "tool_use_id": "t1", "tool_output": "x", "tool_is_error": True}
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


def test_completed_rounds_splits_at_assistant_runs() -> None:
    rounds, trailing = completed_rounds(
        [_user("a"), _assistant("b"), _assistant("c"), _user("d"), _assistant("e"), _user("f")]
    )
    assert [(len(r.consumed), len(r.answer)) for r in rounds] == [(1, 2), (1, 1)]
    assert [m.content[0].text for m in trailing] == ["f"]


def test_completed_rounds_without_an_assistant_is_all_trailing() -> None:
    rounds, trailing = completed_rounds([_text("system", "s"), _user("a")])
    assert rounds == []
    assert len(trailing) == 2


def test_round_hash_is_stable_and_ignores_system_and_reasoning() -> None:
    plain = round_hash([_user("a")], [_assistant("b")])
    noisy = round_hash(
        [_text("system", "s"), _user("a")],
        [
            NormalizedMessage(
                role="assistant",
                content=[NormalizedContent(kind="reasoning", text="r"), NormalizedContent(kind="text", text="b")],
            )
        ],
    )
    assert plain is not None
    assert plain == noisy
    assert plain != round_hash([_user("a")], [_assistant("c")])


def test_round_hash_needs_an_answer() -> None:
    assert round_hash([_user("a")], []) is None
    assert round_hash([_user("a")], [NormalizedMessage(role="assistant", content=[])]) is None


def _history(rounds: int) -> list[NormalizedMessage]:
    out: list[NormalizedMessage] = []
    for i in range(rounds):
        out += [_user(f"u{i}"), _assistant(f"a{i}")]
    return out


def test_first_event_lists_its_own_round_and_the_guard() -> None:
    own, recent = round_links([_user("u0")], _assistant("a0"), depth=10)
    assert own == round_hash([_user("u0")], [_assistant("a0")])
    assert recent == [own, CONVERSATION_START]


def test_links_are_newest_first_and_end_at_the_guard_when_short() -> None:
    own, recent = round_links([*_history(2), _user("u2")], _assistant("a2"), depth=10)
    assert recent[0] == own
    assert recent[1:] == [
        round_hash([_user("u1")], [_assistant("a1")]),
        round_hash([_user("u0")], [_assistant("a0")]),
        CONVERSATION_START,
    ]


def test_the_guard_marks_exactly_the_events_that_reach_round_one() -> None:
    _, tenth = round_links([*_history(9), _user("u9")], _assistant("a9"), depth=10)
    _, eleventh = round_links([*_history(10), _user("u10")], _assistant("a10"), depth=10)
    assert len(tenth) == 11 and tenth[-1] == CONVERSATION_START
    assert len(eleventh) == 10 and CONVERSATION_START not in eleventh


def test_consecutive_events_share_all_but_one_hash() -> None:
    _, a = round_links([*_history(12), _user("u12")], _assistant("a12"), depth=10)
    _, b = round_links([*_history(13), _user("u13")], _assistant("a13"), depth=10)
    assert set(a) & set(b) == set(a[:9])


def test_no_answer_lists_the_complete_rounds_and_no_own_hash() -> None:
    own, recent = round_links([*_history(2), _user("u2")], None, depth=10)
    assert own is None
    assert recent == [
        round_hash([_user("u1")], [_assistant("a1")]),
        round_hash([_user("u0")], [_assistant("a0")]),
        CONVERSATION_START,
    ]


def test_an_empty_answer_counts_as_no_answer() -> None:
    own, recent = round_links([_user("u0")], NormalizedMessage(role="assistant", content=[]), depth=3)
    assert own is None
    assert recent == [CONVERSATION_START]


def test_project_merges_adjacent_messages_of_one_role() -> None:
    merged = _dump([_user("a"), _user("b"), _assistant("c"), _assistant("d")])
    assert merged == [
        {"role": "user", "content": [{"kind": "text", "text": "a"}, {"kind": "text", "text": "b"}]},
        {"role": "assistant", "content": [{"kind": "text", "text": "c"}, {"kind": "text", "text": "d"}]},
    ]


def test_a_response_run_hashes_alike_merged_or_split() -> None:
    one = NormalizedMessage(
        role="assistant",
        content=[NormalizedContent(kind="text", text="x"), NormalizedContent(kind="text", text="y")],
    )
    assert round_hash([_user("a")], [one]) == round_hash([_user("a")], [_assistant("x"), _assistant("y")])


def test_a_response_and_its_replay_in_the_next_request_link_alike() -> None:
    merged = NormalizedMessage(
        role="assistant",
        content=[NormalizedContent(kind="text", text="x"), NormalizedContent(kind="text", text="y")],
    )
    own, _ = round_links([_user("a")], merged, depth=10)
    replay = [_user("a"), _assistant("x"), _assistant("y"), _user("b")]
    _, recent = round_links(replay, _assistant("z"), depth=10)
    assert own in recent


def test_no_transcript_and_no_answer_has_no_links() -> None:
    assert round_links([], None, depth=10) == (None, [])


def test_a_round_whose_answer_projects_empty_is_skipped_and_not_counted() -> None:
    thinking = NormalizedMessage(
        role="assistant", content=[NormalizedContent(kind="reasoning", text="hm")]
    )
    _, recent = round_links([_user("a"), thinking, _user("b")], _assistant("c"), depth=2)
    assert len(recent) == 2 and recent[-1] == CONVERSATION_START


def test_events_stitch_after_up_to_n_minus_m_are_lost() -> None:
    n, m = 10, 4

    def links(k: int) -> list[str]:
        history = [x for i in range(k) for x in (_user(f"u{i}"), _assistant(f"a{i}"))]
        return round_links([*history, _user(f"u{k}")], _assistant(f"a{k}"), depth=n)[1]

    def stitches(a: list[str], b: list[str]) -> bool:
        real_a = [h for h in a if h != CONVERSATION_START]
        real_b = [h for h in b if h != CONVERSATION_START]
        shorter = a if len(real_a) <= len(real_b) else b
        need = min(m, len(real_a), len(real_b)) if CONVERSATION_START in shorter else m
        return len(set(real_a) & set(real_b)) >= need

    for k in range(0, 40):
        for lost in range(0, n - m):
            assert stitches(links(k), links(k + 1 + lost)), (k, lost)
    assert not stitches(links(0), links(n))  # far past the window: nothing shared


def test_a_run_of_assistant_messages_is_one_round_in_the_list() -> None:
    merged = round_links([_user("a"), _assistant("b"), _assistant("c"), _user("d")], _assistant("e"), depth=10)
    split = round_links([_user("a"), _assistant("b"), _user("d")], _assistant("e"), depth=10)
    assert len(merged[1]) == len(split[1])


def test_depth_bounds_the_list() -> None:
    _, recent = round_links([*_history(30), _user("u30")], _assistant("a30"), depth=4)
    assert len(recent) == 4 and CONVERSATION_START not in recent
```

- [ ] **Step 2: Run to verify failure**

Run: `cd shared && uv run pytest tests/test_rounds.py -q`
Expected: collection error, `ModuleNotFoundError: slashid_ai_forwarder_core.rounds`.

- [ ] **Step 3: Implement `rounds.py`**

```python
"""Rounds, and the hashes that let a consumer stitch events into a conversation.

A round is the messages the model consumed and the response it gave. Its hash
covers a projection that survives replay, so the response in event k and the
same message replayed in event k+1's request hash alike.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

# Annotations only: ``normalized.types`` imports ``events`` at load, and
# ``events`` imports this module, so a runtime import here would cycle.
if TYPE_CHECKING:
    from .normalize.normalized.types import NormalizedContent, NormalizedMessage

# Not a digest, so it cannot collide with a real hash.
CONVERSATION_START = "conversation-start"


class _Block(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "tool_use", "tool_result", "attachment"]
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: JsonValue = None
    tool_output: JsonValue = None
    tool_is_error: bool | None = None


class _Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant", "tool"]
    content: list[_Block]


@dataclass(frozen=True)
class Round:
    consumed: list[NormalizedMessage]
    answer: list[NormalizedMessage]


def _project_block(block: NormalizedContent) -> _Block | None:
    match block.kind:
        case "text":
            return _Block(kind="text", text=block.text)
        case "tool_use":
            return _Block(
                kind="tool_use",
                tool_use_id=block.tool_use_id,
                tool_name=block.tool_name,
                tool_input=block.tool_input,
            )
        case "tool_result":
            return _Block(
                kind="tool_result",
                tool_use_id=block.tool_use_id,
                tool_output=block.tool_output,
                tool_is_error=block.tool_is_error,
            )
        case "image" | "audio" | "document":
            return _Block(kind="attachment")
        case _:
            return None


def project(messages: Sequence[NormalizedMessage]) -> list[_Message]:
    """The part of ``messages`` that is the same as a response and as replayed history.

    Adjacent messages of one role merge: a run can be one message in the event
    and several in the next request.
    """
    out: list[_Message] = []
    for message in messages:
        if message.role == "system":
            continue
        blocks = [b for c in message.content if (b := _project_block(c)) is not None]
        if not blocks:
            continue
        if out and out[-1].role == message.role:
            out[-1] = _Message(role=message.role, content=[*out[-1].content, *blocks])
        else:
            out.append(_Message(role=message.role, content=blocks))
    return out


def completed_rounds(
    messages: Sequence[NormalizedMessage],
) -> tuple[list[Round], list[NormalizedMessage]]:
    """Rounds closed by an assistant run, and the messages after the last one."""
    rounds: list[Round] = []
    consumed: list[NormalizedMessage] = []
    answer: list[NormalizedMessage] = []
    for message in messages:
        if message.role == "assistant":
            answer.append(message)
            continue
        if answer:
            rounds.append(Round(consumed, answer))
            consumed, answer = [], []
        consumed.append(message)
    if answer:
        rounds.append(Round(consumed, answer))
        consumed = []
    return rounds, consumed


def round_hash(
    consumed: Sequence[NormalizedMessage], answer: Sequence[NormalizedMessage]
) -> str | None:
    """sha256 of the projected round; ``None`` when there is no answer to hash."""
    if not project(answer):
        return None
    body = [m.model_dump(mode="json", exclude_none=True) for m in project([*consumed, *answer])]
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()


def round_links(
    history: Sequence[NormalizedMessage],
    answer: NormalizedMessage | None,
    *,
    depth: int,
) -> tuple[str | None, list[str]]:
    """``(round_hash, recent_round_hashes)`` for an event.

    ``history`` is the transcript before the response and ``answer`` the
    response, absent for a record with none. The list is newest first, at
    most ``depth`` hashes, and ends with the guard when it reaches round one.
    """
    answered = answer is not None and bool(project([answer]))
    if not history and not answered:
        return None, []
    rounds, trailing = completed_rounds(history)
    rounds = [r for r in rounds if project(r.answer)]
    if answer is not None and answered:
        rounds.append(Round(trailing, [answer]))
    recent = [h for r in rounds[-depth:] if (h := round_hash(r.consumed, r.answer)) is not None]
    recent.reverse()
    if len(rounds) <= depth:
        recent.append(CONVERSATION_START)
    return (recent[0] if answered else None), recent
```

- [ ] **Step 4: Run to verify pass**

Run: `cd shared && uv run pytest tests/test_rounds.py -q && uv run ruff check . && uv run ruff format . && uv run ty check`
Expected: all pass; format may rewrite long test lines, re-run `ruff format --check .`.

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/rounds.py shared/tests/test_rounds.py
git commit -m "feat(shared): round projection, split and hash links"
```

### Task 2: Config settings

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/config_base.py:30-37`
- Test: `shared/tests/test_config_base.py`

- [ ] **Step 1: Write the failing test**

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from slashid_ai_forwarder_core.config_base import BaseConfig


def _config(**env: str) -> BaseConfig:
    return BaseConfig(endpoint="http://test", push_token="t", **env)  # ty: ignore[invalid-argument-type]


def test_input_scope_defaults_to_round_and_depth_to_ten() -> None:
    config = _config()
    assert config.input_scope == "round"
    assert config.round_link_depth == 10


def test_input_scope_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_INPUT_SCOPE", "session")
    monkeypatch.setenv("SLASHID_ROUND_LINK_DEPTH", "4")
    config = _config()
    assert (config.input_scope, config.round_link_depth) == ("session", 4)


def test_input_scope_rejects_unknown_values_and_zero_depth() -> None:
    with pytest.raises(ValidationError):
        _config(input_scope="turn")
    with pytest.raises(ValidationError):
        _config(round_link_depth=0)
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError`/no field): `cd shared && uv run pytest tests/test_config_base.py -q`

- [ ] **Step 3: Implement.** In `config_base.py` add after `max_content_size`:

```python
    # ``round`` hashes only the messages the model consumed; ``session`` the
    # whole transcript before the response.
    input_scope: Literal["session", "round"] = "round"
    # How many rounds ``recent_round_hashes`` lists, own round included.
    round_link_depth: int = Field(10, ge=1)
```

and import `Literal` from `typing` (check the existing import block first).

- [ ] **Step 4: Run, expect PASS.** Also run `uv run ty check` (the test's `# ty: ignore` may be unneeded; remove it if ty reports an unused ignore).

- [ ] **Step 5: Commit** `git commit -m "feat(shared): SLASHID_INPUT_SCOPE and SLASHID_ROUND_LINK_DEPTH"`

### Task 3: Builder and wire fields

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/events.py` (`AIInvocationObservedV1` near `conversation_id`; `build_event_from_normalized`)
- Test: `shared/tests/test_events.py`

- [ ] **Step 1: Write failing tests** (append to `test_events.py`; extend `_config` with `input_scope: str = "round"` and `round_link_depth: int = 10` keyword args passed through to `BaseConfig`). Build `NormalizedInvocation` directly:

```python
def _msg(role: str, text: str) -> NormalizedMessage:
    return NormalizedMessage(role=role, content=[NormalizedContent(kind="text", text=text)])


def _invocation(messages: list[NormalizedMessage], answer: str = "ok") -> NormalizedInvocation:
    return NormalizedInvocation(
        input=NormalizedInvocationInput(messages=messages),
        output=NormalizedInvocationOutput(message=_msg("assistant", answer), stop_reason="end_turn"),
    )


def _plain_envelope() -> EventEnvelope:
    return EventEnvelope(
        request_id="r",
        timestamp="2026-06-01T12:00:00+00:00",
        identity_details=AWSIdentityDetails(principal_arn="arn:aws:iam::1:user/a"),
        model=AIModel(id="m"),
        parsed_as="test",
    )


def _canonical_sha256(body: object) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def test_round_scope_hashes_only_the_consumed_round() -> None:
    history = [_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]
    event = await build_event_from_normalized(_invocation(history), _plain_envelope(), config=_config())
    assert event.input is not None and event.input.content_hashes is not None
    assert event.input.content_hashes["sha256"] == _canonical_sha256(
        [history[2].model_dump(mode="json", exclude_none=True)]
    )


async def test_session_scope_hashes_the_whole_transcript_as_a_message_list() -> None:
    history = [_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]
    event = await build_event_from_normalized(
        _invocation(history), _plain_envelope(), config=_config(input_scope="session")
    )
    assert event.input is not None and event.input.content_hashes is not None
    assert event.input.content_hashes["sha256"] == _canonical_sha256(
        [m.model_dump(mode="json", exclude_none=True) for m in history]
    )


async def test_a_leading_system_message_is_in_round_one_and_not_round_two() -> None:
    first = await build_event_from_normalized(
        _invocation([_msg("system", "sys"), _msg("user", "a")]), _plain_envelope(), config=_config()
    )
    second = await build_event_from_normalized(
        _invocation([_msg("system", "sys"), _msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")]),
        _plain_envelope(),
        config=_config(),
    )
    assert first.input is not None and second.input is not None
    assert first.input.byte_length and second.input.byte_length
    assert first.input.byte_length > second.input.byte_length


async def test_input_does_not_depend_on_declared_tools() -> None:
    bare = _invocation([_msg("user", "a")])
    declared = _invocation([_msg("user", "a")])
    tools, servers = build_tools_declared([("Read", None, None)])
    declared.input.tools_declared, declared.input.tool_servers = tools, servers
    one = await build_event_from_normalized(bare, _plain_envelope(), config=_config())
    two = await build_event_from_normalized(declared, _plain_envelope(), config=_config())
    assert one.input == two.input


async def test_used_tools_still_resolve_against_history_in_round_scope() -> None:
    # the tool_use is one round back; its result is in the consumed round.
    history = [
        _msg("user", "go"),
        NormalizedMessage(
            role="assistant",
            content=[NormalizedContent(kind="tool_use", tool_use_id="t1", tool_name="Read", tool_input={})],
        ),
        NormalizedMessage(
            role="user",
            content=[NormalizedContent(kind="tool_result", tool_use_id="t1", tool_output="x")],
        ),
    ]
    invocation = _invocation(history)
    tools, servers = build_tools_declared([("Read", None, None)])
    invocation.input.tools_declared, invocation.input.tool_servers = tools, servers
    event = await build_event_from_normalized(invocation, _plain_envelope(), config=_config())
    assert event.used_tools is not None and len(event.used_tools) == 1


async def test_event_carries_round_hash_and_recent_hashes() -> None:
    event = await build_event_from_normalized(
        _invocation([_msg("user", "a")]), _plain_envelope(), config=_config()
    )
    assert event.round_hash is not None
    assert event.recent_round_hashes == [event.round_hash, "conversation-start"]


async def test_round_link_depth_comes_from_config() -> None:
    history = [m for i in range(5) for m in (_msg("user", f"u{i}"), _msg("assistant", f"a{i}"))]
    event = await build_event_from_normalized(
        _invocation([*history, _msg("user", "u5")]),
        _plain_envelope(),
        config=_config(round_link_depth=3),
    )
    assert event.recent_round_hashes is not None and len(event.recent_round_hashes) == 3


async def test_an_event_with_no_transcript_carries_no_links() -> None:
    event = await build_event_from_normalized(NormalizedInvocation(), _plain_envelope(), config=_config())
    assert event.round_hash is None and event.recent_round_hashes is None


async def test_an_event_without_an_answer_has_no_round_hash() -> None:
    invocation = NormalizedInvocation(
        input=NormalizedInvocationInput(messages=[_msg("user", "a"), _msg("assistant", "b"), _msg("user", "c")])
    )
    event = await build_event_from_normalized(invocation, _plain_envelope(), config=_config())
    assert event.round_hash is None
    assert event.recent_round_hashes is not None and event.recent_round_hashes[-1] == "conversation-start"
```

Add the imports the tests need (`NormalizedContent`, `NormalizedMessage`, `NormalizedInvocation`, `NormalizedInvocationInput`, `NormalizedInvocationOutput` from `normalize.normalized.types`; `build_tools_declared` from `normalize.normalized.tools`, confirm its name and signature with `grep -n "def build_tools_declared" -r shared/src`; `EventEnvelope`, `AIModel`, `AWSIdentityDetails` are likely already imported).

- [ ] **Step 2: Run, expect FAIL** (`round_hash` attribute missing; hash mismatch): `cd shared && uv run pytest tests/test_events.py -q -k "round or scope or declared_tools or still_resolve"`

- [ ] **Step 3: Implement.**

In `AIInvocationObservedV1`, after `conversation_id`:

```python
    # Stitching hashes: see rounds.py. ``round_hash`` is absent when the
    # event has no response; the list is newest first and ends with
    # ``"conversation-start"`` when it reaches the first round.
    round_hash: str | None = None
    recent_round_hashes: list[str] | None = None
```

In `build_event_from_normalized`, import `after_last_assistant` (from `.normalize.turn`; check for an import cycle, `finalize.py` already imports `..events`, so import inside the module that events.py already depends on, or at function level if a cycle appears) and `round_links` (from `.rounds`). Replace the `input=` argument and add the fields:

```python
    messages = normalized.input.messages
    scoped = after_last_assistant(messages) if config.input_scope == "round" else messages
    round_hash, recent = round_links(
        messages, normalized.output.message, depth=config.round_link_depth
    )
    ...
        input=_build_content(
            [m.model_dump(mode="json", exclude_none=True) for m in scoped],
            include_text=config.include_raw_content,
            max_content_size=config.max_content_size,
        ),
    ...
        round_hash=round_hash,
        recent_round_hashes=recent or None,
```

`_used_tools` and `finalize` keep reading `normalized.input.messages`: only the hashed body is sliced. Update the builder docstring's `input` sentence to say it hashes the messages only. Keep `_strip_empty_top` (still used for `output`).

- [ ] **Step 4: Run the shared suite**

Run: `cd shared && uv run pytest -q`
Expected: the new tests pass. Existing tests that encode old `input` hashes or the old `{"messages": …}` body now fail; list them with `uv run pytest -q 2>&1 | grep FAILED`.

- [ ] **Step 5: Fix the old expectations.** For each failing test, confirm from the diff that only the `input` hash (and, with `include_raw_content`, `redacted_text`/`byte_length`) changed, then update the expected value to the new output. If a test asserts `redacted_text` equals `{"messages": [...]}` JSON, change it to the bare array. Do not loosen an assertion to make it pass.

- [ ] **Step 6: Lint, type check, commit**

```bash
cd shared && uv run ruff check . && uv run ruff format . && uv run ty check && uv run pytest -q
git add -A shared && git commit -m "feat(shared): hash only messages in input; add round_hash and recent_round_hashes"
```

A hook-built record and a reader-built record at one address replace each other's whole `event`, so the stored hashes are the last writer's. That is the spec's known limitation (hook and reader rounds do not stitch); no code change.

## Chunk 2: forwarders and docs

### Task 4: Forwarder suites

**Files:** tests and fixtures only, unless a failure shows a real defect.

- [ ] **Step 1: Run each suite**

```bash
for p in bedrock vertex anthropic; do (cd $p && uv run pytest -q 2>&1 | tail -15); done
```

Expected: failures only where tests pin an `input` hash or a whole-event dict. Check at least: `anthropic/tests/test_write_from_frame.yaml`, `test_produced_runs.yaml`, `test_partial_event.yaml`, `test_preflight.py`, `test_record.py`, `test_envelope.py`, `test_softjoin.py`, `shared/tests/normalize/test_gemini_to_normalized_invocation.yaml`, `shared/tests/normalize/normalized/test_tool_results.yaml`, and the vertex audit-only tests (an event with no transcript must carry neither new field). Baseline each suite on `main` first if unsure (`git stash` is not needed; use `git worktree` of main or read the failure diff).

- [ ] **Step 2: Update expectations.** Same rule as Task 3 step 5: each diff must be explained by the spec (input slice/array shape, or the two new fields). Regenerate YAML values by running the function and pasting the new output; read every changed line.

- [ ] **Step 3: Anthropic tail and denial records.** In `anthropic/tests/test_pending.py`, extend the test that builds a tail via `unanswered_round` (search `unanswered_round` and `test_a_tail_is_never_pushed_on_arrival`) or add a new one:

```python
async def test_a_tail_has_no_round_hash_but_lists_the_complete_rounds() -> None:
    f = <the multi-round frame fixture the neighbouring tests use>
    tail = await unanswered_round(f, webhook_id="wh", signed_at=SIGNED_AT, config=settings)
    assert tail is not None
    assert tail.round_hash is None and tail.output is None
    assert tail.recent_round_hashes and tail.recent_round_hashes[0] != "conversation-start"
```

Use a frame whose trailing assistant run is two messages (build one from a fixture by splitting the last assistant message in two) so the merge is covered, and pick a frame with at least one assistant message (e.g. `frame_tool_result.json`; see how neighbouring tests load frames) and assert that `recent_round_hashes[0]` equals the `round_hash` of the event `partial_event` emits for the same frame, since the tail's newest complete round is that event's round. Also assert, for the denial record built from the same tail, that `round_hash is None`.

- [ ] **Step 4: Run all suites and the repo checks**

```bash
for p in shared bedrock vertex anthropic; do (cd $p && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q) || echo "FAILED $p"; done
```

Expected: no `FAILED`.

- [ ] **Step 5:** Update stale comments: `anthropic/src/slashid_anthropic_forwarder/record.py` `_elide_input` ("The input is the whole transcript") and the `input` sentences in the `partial_event` and builder docstrings, at their current length.

- [ ] **Step 6: Commit** `git commit -am "test: pin the new input shape and round hashes in the forwarders"`

### Task 5: README rows and cross-references

**Files:** `anthropic/README.md:320`, `vertex/README.md:150`; `bedrock/README.md` only if it has an env table (check with `grep -n "SLASHID_" bedrock/README.md`).

- [ ] **Step 1:** Next to `SLASHID_INCLUDE_RAW_CONTENT`, add rows in the same table format:

```
| `SLASHID_INPUT_SCOPE` | no | `round` |
| `SLASHID_ROUND_LINK_DEPTH` | no | `10` |
```

Match the table's column meaning (read the header and one neighbouring row's description cell and write equally terse descriptions: `round` = messages since the last response, `session` = whole transcript; N rounds in `recent_round_hashes`).

- [ ] **Step 2:** Add one sentence to each README's behaviour section that describes `input`, if one exists (`grep -n "input" <README>`), saying it is the messages since the last response by default.

- [ ] **Step 3: Commit** `git commit -am "docs: document SLASHID_INPUT_SCOPE and SLASHID_ROUND_LINK_DEPTH"`

### Task 6: Verify and open the PR

- [ ] **Step 1:** Re-run Task 4 step 4 from a clean state; confirm `git status` is clean and `uv lock --check` (or `uv sync --locked`) passes.
- [ ] **Step 2: Review.** Use superpowers:requesting-code-review against the spec; fix findings.
- [ ] **Step 3: Push and open the PR** (no merge, ever; wait for explicit approval):

```bash
git push -u origin docs/input-scope-round-hashes
gh pr create --title "feat: input is the round's messages; add round hashes for stitching" --body "<summary, hash-change note, tests, attribution line>"
```

The body must say: `input.content_hashes` changes for every consumer (scope default and tools no longer hashed); the two new optional wire fields need the end-of-Phase-2 schema sync; `output` is unchanged; Codex will add the windowed-history flag. Then invoke the `pull-request-assist:pr-workflow` skill.
