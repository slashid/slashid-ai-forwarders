# Anthropic Inference Hooks Receiver Implementation Plan

> **SUPERSEDED (2026-09-21) by `2026-09-21-anthropic-ai-invocations.md`.** This plan was written against the stateless emit-previous receiver, where every frame pushed the previous round's event directly and `SLASHID_ENFORCE` gated denials. The design has since moved to a pending store with a completion predicate and a deadline flush, a second compliance capability with two readers, and `SLASHID_SHADOW_MODE`. Chunk 1 landed and is still accurate; Chunks 2 and 4 were carried into the new plan largely intact; Chunks 3, 5 and 6 were rewritten. Kept for its captured-fixture notes and its record of what shipped.


> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stateless FastAPI service on Cloud Run that receives Claude Enterprise Inference Hooks, composes an allow/deny verdict from the Go policy receiver and the SlashID preflight endpoint, and pushes `AIInvocationObservedV1` events one round behind.

**Architecture:** New uv workspace member `anthropic/` beside `bedrock/` and `vertex/`. The frame's `messages` parse with the shared Anthropic schema (two small additions), the event is built by the shared normalizer plus `finalize` and `build_event_from_normalized`, and every response is a pure function of the frame in hand. Two checks run concurrently under one budget; the push runs after the response and can never change it.

**Tech Stack:** Python 3.13, uv workspace, pydantic 2.13, FastAPI + uvicorn, httpx, pytest + pytest-asyncio (auto mode), ruff (line-length 100), ty, Docker (wolfi), Cloud Run v2 via Terraform.

**Spec:** `mcp-agent/docs/superpowers/specs/2026-09-18-anthropic-inference-hooks-design.md` (read it first; its "Observed wire shapes" section is what the fixtures encode).

---

## Before you start

Five decisions already made; reversing one silently breaks something:

1. **Stateless.** No session store. The frame is cumulative; frame N+1 carries everything invocation N needs.
2. **Consumption attribution.** `used_tools` and `accessed_files` on invocation N come from the round the model consumed (`U_prev`), exactly as Bedrock and Vertex do. The session's last response is never reported. Do not "fix" this.
3. **An eventing failure never becomes a verdict failure.** The push runs after the response in a tracked task; a non-200 from us is a webhook failure that hands control to the customer's fail-open/fail-closed setting.
4. **Unknown top-level `type` and `config-test` frames bypass both checks and answer allow.** The Go receiver denies both.
5. **Denial records exist only under `SLASHID_ENFORCE=true`.** Observe-only logs the would-be deny; emitting it would double-record the turn.
6. **The signature crypto is not ours.** `signature.py` wraps the `standardwebhooks` reference library, which owns the HMAC, the `whsec_` prefix, the base64 alphabet and the ±300 s tolerance. The wrapper exists only to accept several secrets during a rotation and to answer with a bool. Do not reimplement it: the base64-alphabet trap the protocol documentation warns about is the library's problem now.

Toolchain, from the subproject directory (what CI runs):

```bash
cd anthropic && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
cd shared    && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
```

Fixtures under `anthropic/tests/fixtures/` are sanitized captures from a live tenant (2026-09-20). `frame_mcp_tool.json` holds two consecutive user messages and an `mcp__demo__echo` call. `frame_subagent_parent.json` has no case of its own; it is kept as the parent-side counterpart of `frame_subagent_child.json` for protocol reference. Their shapes are authoritative; when a test disagrees with a fixture, the test is wrong.

## File structure

```
shared/src/slashid_ai_forwarder_core/
├── events.py                                  # + AnthropicIdentityDetails in the union
└── normalize/anthropic/
    ├── schema.py                              # + AliasChoices on tool_use.name; + AnthropicAttachmentBlock
    └── normalize.py                           # + translate_request_messages(); attachment → document

anthropic/
├── pyproject.toml, Dockerfile, README.md      # (exist; pyproject declares the pyyaml dev group)
├── deploy/
│   ├── cloudbuild.yaml, dev-deploy.sh         # (exist) dev iteration path
│   └── terraform/                             # customer path
├── src/slashid_anthropic_forwarder/
│   ├── config.py                              # (exists)
│   ├── signature.py                           # (exists) wraps the standardwebhooks library
│   ├── capture.py                             # (exists) raw-frame capture, test tenants only
│   ├── frame.py                               # PromptFrame envelope + split_transcript()
│   ├── checks.py                              # Verdict, CheckFailed — shared by the two clients and the composer
│   ├── policy.py                              # forward raw frame to the Go receiver
│   ├── preflight.py                           # POST /ip/nhi/ai/preflight
│   ├── verdict.py                             # decide(): concurrency, budget, fail mode, enforce
│   ├── event_envelope.py                      # accessed_files_for(), previous_invocation_event(), denial_event()
│   └── main.py                                # (exists; rewritten in chunk 5) route + push isolation
└── tests/
    ├── __init__.py, conftest.py               # (exist) package marker + signer fixture
    ├── fixtures/*.json                        # (exist) nine captured frames
    ├── test_config.py, test_signature.py, test_main.py   # (exist)
    ├── test_frame.py            + test_split_transcript.yaml
    ├── test_event_envelope.py   + test_accessed_files_for.yaml,
    │                              test_previous_invocation_event.yaml, test_denial_event.yaml
    ├── test_verdict.py          + test_decide.yaml
    ├── test_policy.py, test_preflight.py
```

The `.yaml` files are `yaml_pytest` case tables, the house pattern from `shared/tests`; each is named after the test function it parametrizes and must be committed with it, or the module raises `FileNotFoundError` at import. `tests/__init__.py` is what makes `from tests.conftest import SECRET` resolve.

`checks.py` is one file the spec's layout does not list. `policy.py` and `preflight.py` both return a verdict and both raise the same failure, and `verdict.py` imports both, so the shared types cannot live in any of the three without a cycle.

---

## Chunk 1: Scaffold, config, signature, capture receiver — DONE

Landed on `paulo/anthropic-receiver` (commits `4b2e303`, `c36b2ab`, `5cd5b66`, `e9cbf96`, `cc4b156`, `95a5062`, `5c5ca22`). Recorded so the plan is complete; nothing to do. A clean checkout of `main` does **not** have these: start from the branch.

- [x] `anthropic/pyproject.toml`, root workspace member, `Config(BaseConfig)` with `signing_secrets`, fail mode, budgets, `enforce`, `max_body_bytes`, capture knobs, and the unsigned+policy-URL rejection (`tests/test_config.py`, 5 tests).
- [x] `signature.py` Standard Webhooks verification, delegating the crypto to the `standardwebhooks` reference library (`tests/test_signature.py`, 10 tests, unchanged across the swap).
- [x] `capture.py` + `main.py` capture receiver: 401 unsigned, 413 oversized, allow on unknown type, deny marker under enforce, capture failure isolated (`tests/test_main.py`, 10 tests).
- [x] `Dockerfile`, `deploy/cloudbuild.yaml`, `deploy/dev-deploy.sh`; deployed to `strong-hue-507702-k7` and driven with `claude-work`; nine sanitized fixtures written, `frame_mcp_tool.json` among them.
- [x] `uv.lock` updated for the new member, and `pyyaml` added to `anthropic`'s dev group — every `yaml_pytest` suite in chunks 3 and 4 needs it.

---

## Chunk 2: Shared library additions

### Task 2.1: `tool_use.name` accepts the hook's `tool_name`

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/schema.py` (class `AnthropicToolUseBlock`, and the `from pydantic import` line)
- Test: `shared/tests/normalize/test_schemas_anthropic.py`

- [ ] **Step 1: Write the failing test** — append to `test_schemas_anthropic.py`:

```python
def test_tool_use_block_accepts_hook_spelling_of_name() -> None:
    """The Inference hooks frame spells the tool name ``tool_name``; the
    Messages API spells it ``name``. One field, two wire keys."""
    block = AnthropicToolUseBlock.model_validate(
        {"type": "tool_use", "id": "toolu_1", "tool_name": "Read", "input": {"file_path": "a"}}
    )
    assert block.name == "Read"
    assert AnthropicToolUseBlock(type="tool_use", id="toolu_2", name="Bash").name == "Bash"


def test_request_union_picks_tool_use_for_hook_spelling() -> None:
    from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage

    msg = AnthropicRequestMessage.model_validate(
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "tool_name": "Read"}]}
    )
    assert isinstance(msg.content[0], AnthropicToolUseBlock)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run pytest tests/normalize/test_schemas_anthropic.py -k hook_spelling -v`
Expected: FAIL — a `ValidationError` reporting `name` / `Field required` on the first test; the second fails because the block validates as `AnthropicUnknownBlock`.

- [ ] **Step 3: Implement** — in `schema.py` change the pydantic import to `from pydantic import AliasChoices, Field, JsonValue` and the class to:

```python
class AnthropicToolUseBlock(_LenientModel):
    type: Literal["tool_use"]
    id: str
    # The Messages API spells it ``name``; the Inference hooks frame ``tool_name``.
    name: str = Field(validation_alias=AliasChoices("name", "tool_name"))
    input: JsonValue = None
```

- [ ] **Step 4: Run to verify it passes, and nothing else broke**

Run: `cd shared && uv run pytest -q`
Expected: all pass (the pre-existing schema and normalize suites construct the block with `name=`, which `AliasChoices` still accepts).

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/anthropic/schema.py shared/tests/normalize/test_schemas_anthropic.py
git commit -m "feat(shared): tool_use.name accepts the Inference hooks tool_name spelling"
```

### Task 2.2: `AnthropicAttachmentBlock` and its translation to `document`

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/schema.py` (new class, `AnthropicRequestContentBlock` union)
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/normalize.py` (`_translate_request_content`, imports)
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/normalized/media_types.py` (`parse_media_type`)
- Test: `shared/tests/normalize/test_anthropic_normalize.py`, `shared/tests/normalize/normalized/test_parse_media_type.yaml`

`parse_media_type` claims to return `None` for an unregistered value, but `MimeType(...)` is a plain `str` constructor that never raises; the IANA check runs only inside pydantic validation. So today an unregistered value survives the parser and then blows up `NormalizedContent`. Fix the parser first; it has no other production call sites.

- [ ] **Step 1: Write the failing tests** — append a case to `test_parse_media_type.yaml`:

```yaml
---
id: unregistered
raw: not/a-real-type
expected: null
```

and append to `test_anthropic_normalize.py`:

```python
async def test_attachment_block_becomes_document_sized_by_its_text() -> None:
    """Hook attachments carry extracted text, never bytes. ``byte_length`` is
    the length of that text — the same bytes the receiver hashes — not the
    frame's ``size_bytes``, which is null for text uploads anyway."""
    from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
        message_to_normalized_invocation,
    )
    from slashid_ai_forwarder_core.normalize.anthropic.schema import (
        AnthropicMessage,
        AnthropicRequestBody,
    )

    request = AnthropicRequestBody.model_validate(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "attachment", "file_name": "q.txt", "media_type": "text/plain",
                         "size_bytes": None, "text": "hello\n"},
                        {"type": "attachment", "file_name": None, "media_type": "image/jpeg",
                         "size_bytes": 70657, "text": None},
                        {"type": "attachment", "file_name": None, "media_type": "not/a-real-type",
                         "size_bytes": None, "text": "x"},
                    ],
                }
            ]
        }
    )
    response = AnthropicMessage.model_validate(
        {"type": "message", "role": "assistant", "content": [], "stop_reason": "end_turn"}
    )
    normalized = await message_to_normalized_invocation(request, response, config=_config())
    blocks = normalized.input.messages[0].content
    assert [b.kind for b in blocks] == ["document", "document", "document"]
    assert blocks[0].text == "hello\n"
    assert blocks[0].byte_length == 6
    assert blocks[0].media_type == "text/plain"
    assert blocks[1].text is None and blocks[1].byte_length is None
    assert blocks[2].media_type is None  # unregistered value tolerated, not fatal
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run pytest tests/normalize/normalized/test_parse_media_type.py tests/normalize/test_anthropic_normalize.py -k "unregistered or attachment_block" -v`
Expected: FAIL twice — the yaml case gets `not/a-real-type` back instead of `None`; the normalize test's `kind` list is `[]` because the blocks validated as `AnthropicUnknownBlock` and were skipped.

- [ ] **Step 3: Implement**

In `media_types.py`, validate through pydantic instead of the bare constructor:

```python
from pydantic import TypeAdapter, ValidationError
from pydantic_extra_types.mime_types import MimeType

_MIME = TypeAdapter(MimeType)


def parse_media_type(raw: str | None) -> MimeType | None:
    ...  # keep the parameter/whitespace stripping; rewrite the two docstring
    #      sentences that say registry validity is not enforced: it now is,
    #      through TypeAdapter(MimeType), and unregistered values return None.
    try:
        return _MIME.validate_python(base)
    except ValidationError:
        log.debug("unrecognized media type: %r", raw)
        return None
```

In `schema.py`, after `AnthropicToolResultBlock`:

```python
class AnthropicAttachmentBlock(_LenientModel):
    """Inference hooks attachment: metadata plus extracted text, never bytes.

    Every field but ``type`` can be null — an image arrives with no name
    and no text, a PDF with text but no name.
    """

    type: Literal["attachment"]
    file_name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    text: str | None = None
```

and widen the request-side union (keep `AnthropicUnknownBlock` last):

```python
AnthropicRequestContentBlock = (
    AnthropicTextBlock
    | AnthropicToolUseBlock
    | AnthropicThinkingBlock
    | AnthropicToolResultBlock
    | AnthropicAttachmentBlock
    | AnthropicUnknownBlock
)
```

In `normalize.py`, import `AnthropicAttachmentBlock` from `.schema` and `parse_media_type` from `..normalized.media_types`, then add a case to `_translate_request_content` before the unknown-block comment:

```python
            case AnthropicAttachmentBlock():
                out.append(
                    NormalizedContent(
                        kind="document",
                        text=block.text,
                        media_type=parse_media_type(block.media_type),
                        byte_length=len(block.text.encode()) if block.text is not None else None,
                    )
                )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd shared && uv run pytest -q && uv run ty check`
Expected: all pass, including the existing `parse_media_type` cases (registered values still validate).

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/ shared/tests/normalize/test_anthropic_normalize.py shared/tests/normalize/normalized/test_parse_media_type.yaml
git commit -m "feat(shared): Inference hooks attachment block normalizes to a document"
```

### Task 2.3: `translate_request_messages()` public helper

The receiver needs the request-side translation without a response (the preflight hash set is computed before any event exists). Expose it instead of importing a private function across packages.

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/normalize.py` (`_request_to_input`)
- Test: `shared/tests/normalize/test_anthropic_normalize.py`

- [ ] **Step 1: Write the failing test**

```python
def test_translate_request_messages_is_the_request_side_walk() -> None:
    from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
        translate_request_messages,
    )
    from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage

    messages = [
        AnthropicRequestMessage.model_validate(
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "tool_name": "Read", "input": {"file_path": "a"}}]}
        ),
        AnthropicRequestMessage.model_validate(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "1\tx\n", "is_error": False}]}
        ),
    ]
    out = translate_request_messages(messages)
    assert [m.role for m in out] == ["assistant", "user"]
    assert out[0].content[0].kind == "tool_use" and out[0].content[0].tool_name == "Read"
    assert out[1].content[0].kind == "tool_result" and out[1].content[0].tool_output == "1\tx\n"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run pytest tests/normalize/test_anthropic_normalize.py -k translate_request_messages -v`
Expected: FAIL — `ImportError`.

- [ ] **Step 3: Implement** — add above `_request_to_input` and make `_request_to_input` call it:

```python
def translate_request_messages(
    messages: list[AnthropicRequestMessage],
) -> list[NormalizedMessage]:
    """Request-side walk only: conversation turns → canonical messages.

    The Inference hooks receiver uses this on its own to hash the fresh
    round before any response exists.
    """
    return [
        NormalizedMessage(role=msg.role, content=_translate_request_content(msg.content))
        for msg in messages
    ]
```

and in `_request_to_input`, replace the `for msg in request.messages:` loop with `messages.extend(translate_request_messages(request.messages))`.

- [ ] **Step 4: Run** — `cd shared && uv run ruff format . && uv run ruff check --fix . && uv run pytest -q && uv run ty check`. Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/anthropic/normalize.py shared/tests/normalize/test_anthropic_normalize.py
git commit -m "feat(shared): expose the Anthropic request-side translation"
```

### Task 2.4: `AnthropicIdentityDetails` in the identity union

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/events.py` (next to `GCPIdentityDetails`; the `IdentityDetails` alias)
- Test: `shared/tests/test_events.py`

- [ ] **Step 1: Write the failing test** — append:

```python
def test_anthropic_identity_details_round_trips_through_the_union() -> None:
    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    event = AIInvocationObservedV1.model_validate(
        {
            "request_id": "r",
            "timestamp": "2026-09-18T00:00:00Z",
            "identity_details": {"kind": "anthropic", "user_id": "user_01Abc"},
            "model": {"id": "claude-sonnet-4-5"},
            "parsed_as": "anthropic-inference-hook",
        }
    )
    assert isinstance(event.identity_details, AnthropicIdentityDetails)
    assert event.identity_details.user_id == "user_01Abc"
    # The Go resolver requires one of the identifiers and ignores ``kind``.
    assert event.model_dump(exclude_none=True)["identity_details"] == {
        "kind": "anthropic",
        "user_id": "user_01Abc",
    }
```

- [ ] **Step 2: Run to verify it fails** — `cd shared && uv run pytest tests/test_events.py -k anthropic_identity -v`. Expected: FAIL, union has no `anthropic` tag.

- [ ] **Step 3: Implement** — after `GCPIdentityDetails`:

```python
class AnthropicIdentityDetails(_WireModel):
    """Anthropic-source shape of ``AIInvocationObservedV1.identity_details``.

    The Inference hooks frame names the acting principal as ``actor.id``,
    a ``user_01…`` identifier stable across requests. The server's
    resolver accepts ``service_account_id``, ``user_id`` or ``api_key_id``
    and rejects a payload with none of them, so the receiver drops events
    whose actor id is null rather than emit one. ``kind`` is a client-side
    discriminator; the server ignores it.
    """

    kind: Literal["anthropic"] = "anthropic"
    user_id: str | None = None
    service_account_id: str | None = None
    api_key_id: str | None = None
```

and widen the alias:

```python
IdentityDetails = Annotated[
    AWSIdentityDetails | GCPIdentityDetails | AnthropicIdentityDetails,
    Field(discriminator="kind"),
]
```

- [ ] **Step 4: Run** — `cd shared && uv run pytest -q && uv run ty check`. Expected: pass, including the existing AWS/GCP identity tests.

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/events.py shared/tests/test_events.py
git commit -m "feat(shared): AnthropicIdentityDetails in the identity_details union"
```

---

## Chunk 3: Frame and event reconstruction

### Task 3.1: `PromptFrame` and `split_transcript`

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/frame.py`
- Test: `anthropic/tests/test_frame.py`, `anthropic/tests/test_split_transcript.yaml`

- [ ] **Step 1: Write the failing tests**

```python
"""Prompt-frame envelope and the transcript split every path relies on."""

from __future__ import annotations

import json
import pathlib

from typing import Any

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicRequestMessage,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
)
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.frame import PromptFrame, Source, split_transcript

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> PromptFrame:
    return PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))


def test_tool_result_frame_parses_with_shared_blocks() -> None:
    frame = load("frame_tool_result")
    assert frame.type == "prompt"
    assert frame.actor.id == "user_01AbCdEfGhIjKlMnOpQrStUv"
    assert frame.source.application == "claude-code"
    use = frame.messages[1].content[1]
    assert isinstance(use, AnthropicToolUseBlock) and use.name == "Read"
    result = frame.messages[2].content[0]
    assert isinstance(result, AnthropicToolResultBlock) and result.tool_use_id == use.id


def test_attachment_frame_parses_attachments() -> None:
    frame = load("frame_attachment")
    kinds = [type(b).__name__ for b in frame.messages[0].content]
    assert kinds.count("AnthropicAttachmentBlock") == 3
    pdf = [b for b in frame.messages[0].content if isinstance(b, AnthropicAttachmentBlock)][2]
    assert pdf.file_name is None and pdf.text is not None


def test_forward_compatible_shapes_parse() -> None:
    frame = PromptFrame.model_validate(
        {
            "type": "response",
            "request_id": "r",
            "tenant_id": None,
            "actor": {"type": "robot", "id": None, "email_address": None},
            "source": {"application": "brand-new-surface"},
            "messages": [{"role": "user", "content": [{"type": "sparkle", "glitter": 1}]}],
            "metadata": {"unexpected": "key"},
            "brand_new_top_level": True,
        }
    )
    assert frame.type == "response"
    assert frame.actor.id is None
    assert frame.session_id is None and frame.model is None
    assert frame.messages[0].content[0].type == "sparkle"


def test_connection_test_is_recognised() -> None:
    frame = load("frame_first_turn").model_copy(update={"source": Source(application="config-test")})
    assert frame.is_connection_test()
    assert not load("frame_first_turn").is_connection_test()


@yaml_pytest(filename="test_split_transcript.yaml")
def test_split_transcript(
    fixture: str, append: list[dict[str, Any]], before: int, assistant: int, fresh: int
) -> None:
    messages = load(fixture).messages + [AnthropicRequestMessage.model_validate(m) for m in append]
    split = split_transcript(messages)
    assert (len(split.before), len(split.assistant), len(split.fresh)) == (before, assistant, fresh)
    assert split.before + split.assistant + split.fresh == messages
```

with `tests/test_split_transcript.yaml`:

```yaml
id: first_turn_has_no_assistant_run
fixture: frame_first_turn
append: []
before: 0
assistant: 0
fresh: 1
---
id: three_messages
fixture: frame_tool_result
append: []
before: 1
assistant: 1
fresh: 1
---
id: five_messages_take_the_last_assistant_run
fixture: frame_subagent_child
append: []
before: 3
assistant: 1
fresh: 1
---
# Deferred-tool loading: a tool_result message, then a separate "Tool loaded."
# user message, before the assistant's next turn.
id: consumed_round_can_span_two_user_messages
fixture: frame_mcp_tool
append: []
before: 4
assistant: 1
fresh: 1
---
id: consecutive_assistant_messages_are_one_run
fixture: frame_first_turn
append:
  - {role: assistant, content: [{type: text, text: first}]}
  - {role: assistant, content: [{type: text, text: second}]}
  - {role: user, content: [{type: text, text: go on}]}
before: 1
assistant: 2
fresh: 1
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_frame.py -v`. Expected: FAIL, module not found.

- [ ] **Step 3: Implement**

```python
"""Wire model for the Inference hooks prompt frame, and the transcript split.

The envelope tolerates unknown fields and discriminator values: the
protocol grows by addition, and rejecting a request over something new
is a webhook failure. ``messages`` reuse the shared Anthropic schema; a
hook transcript is the Messages API content model plus attachments. That
schema pins ``role`` to user/assistant, so a frame with a new role would
fail to parse and be answered allow by ``main.py``'s parse guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow")


class Actor(_Lenient):
    # Union discriminated on type; "user" is the only value sent today.
    type: str
    id: str | None = None
    email_address: str | None = None


class Source(_Lenient):
    # Open string: claude-ai, claude-code, cowork, config-test, and values
    # not yet invented. Advisory routing metadata, not a trust boundary.
    application: str | None = None


class PromptFrame(_Lenient):
    type: str
    request_id: str
    tenant_id: str | None = None
    actor: Actor = Field(default_factory=lambda: Actor(type="unknown"))
    source: Source = Field(default_factory=Source)
    messages: list[AnthropicRequestMessage] = Field(default_factory=list)
    session_id: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_connection_test(self) -> bool:
        """Anthropic's synthetic probe: the Test connection button and the
        circuit breaker's recovery checks. Carries no user content."""
        return self.source.application == "config-test"


@dataclass(frozen=True)
class Split:
    """``[… before …][ assistant run ][ fresh ]``.

    ``assistant`` is the last run of consecutive assistant messages, the
    output of the previous invocation. ``before`` is its input. ``fresh``
    is what has not reached the model yet, and is what the verdict scans.
    """

    before: list[AnthropicRequestMessage]
    assistant: list[AnthropicRequestMessage]
    fresh: list[AnthropicRequestMessage]


def split_transcript(messages: list[AnthropicRequestMessage]) -> Split:
    fresh = list(after_last_assistant(messages))
    head = messages[: len(messages) - len(fresh)]
    start = len(head)
    while start > 0 and head[start - 1].role == "assistant":
        start -= 1
    return Split(before=head[:start], assistant=head[start:], fresh=fresh)
```

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_frame.py -v && uv run ty check`. Expected: 9 pass.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/frame.py anthropic/tests/test_frame.py anthropic/tests/test_split_transcript.yaml
git commit -m "feat(anthropic): prompt-frame envelope over the shared Anthropic schema"
```

### Task 3.2: `accessed_files_for` — attachments and read results, one recipe for both paths

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/event_envelope.py`
- Test: `anthropic/tests/test_event_envelope.py`, `anthropic/tests/test_accessed_files_for.yaml`

- [ ] **Step 1: Write the failing tests**

```python
"""Event reconstruction: one round behind, consumption-attributed, plus the
enforced-denial record and the file-hash set the verdict shares with it."""

from __future__ import annotations

import json
import pathlib
from typing import Any

from pydantic import BaseModel
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.event_envelope import (
    accessed_files_for,
    attachment_files,
    content_request_id,
)
from slashid_anthropic_forwarder.frame import PromptFrame, split_transcript
from tests.conftest import SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SIGNED_AT = 1789945700


def load(name: str) -> PromptFrame:
    return PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))


def config(**overrides: Any) -> Config:
    return Config(endpoint="https://api.slashid.com", push_token="t", hook_signing_secret=SECRET, **overrides)


def assistant_text(text: str) -> AnthropicRequestMessage:
    return AnthropicRequestMessage.model_validate(
        {"role": "assistant", "content": [{"type": "text", "text": text}]}
    )


# --- accessed_files_for -----------------------------------------------------


class ExpectedFile(BaseModel):
    name: str | None
    sha256: str
    media_type: str | None = None
    byte_length: int | None = None


def check_files(files: list[AIAccessedFile] | None, expected: list[ExpectedFile]) -> None:
    got = files or []
    assert [(f.name, (f.content_hashes or {}).get("sha256")) for f in got] == [
        (e.name, e.sha256) for e in expected
    ]
    for g, e in zip(got, expected, strict=True):
        assert g.content_hashes is not None and set(g.content_hashes) == {"sha256", "sha1", "md5"}
        if e.media_type is not None:
            assert g.media_type == e.media_type
        if e.byte_length is not None:
            assert g.byte_length == e.byte_length


@yaml_pytest(filename="test_accessed_files_for.yaml")
def test_accessed_files_for(fixture: str, expected: list[ExpectedFile]) -> None:
    check_files(accessed_files_for(load(fixture).messages, config=config()), expected)


def test_attachment_redacted_content_follows_include_raw_content() -> None:
    files = attachment_files(load("frame_attachment").messages, config=config(include_raw_content=True))
    # The wire model strips surrounding whitespace on strings; hashes and
    # byte_length are still over the unstripped bytes (yaml cases).
    assert files[0].redacted_content == "Maria tinha um carneirinho"
    assert accessed_files_for(load("frame_attachment").messages, config=config())[0].redacted_content is None


# --- content_request_id ------------------------------------------------------


def test_request_id_anchors_on_the_first_tool_use_id() -> None:
    frame = load("frame_tool_result")
    assert content_request_id(split_transcript(frame.messages), frame.session_id) == "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def test_request_id_fallback_is_prefixed_and_stable_across_reveals() -> None:
    frame = load("frame_after_shadow_deny")  # A is pure text
    a = content_request_id(split_transcript(frame.messages), frame.session_id)
    longer = [*frame.messages, assistant_text("more"), frame.messages[2]]
    # Re-reveal of the same run from a later frame: same before, same run.
    same_run = split_transcript(longer[:3])
    assert a.startswith("hook:") and len(a) == len("hook:") + 32
    assert content_request_id(same_run, frame.session_id) == a
    assert content_request_id(split_transcript(longer), frame.session_id) != a
```

with `tests/test_accessed_files_for.yaml`:

```yaml
id: read_result_hashed_after_stripping_line_numbers
fixture: frame_tool_result
expected:
  - {name: /home/alice/proj/notes.txt, sha256: e6bab19e50f90145e62b963a3584ec72ea6b88adcb71de6522301ebb9fa0813d}
---
id: error_result_is_not_a_file
fixture: frame_tool_error
expected: []
---
id: parallel_reads_yield_one_file_each
fixture: frame_multi_tool
expected:
  - {name: /home/alice/proj/a.txt, sha256: 49e5ab7eb50b84ab791a464363506fca587318ec5400cfe9d9f08ef43f22103a}
  - {name: /home/alice/proj/b.txt, sha256: 200893a46bdff328ad1c118adfe70d737601b81b7062f143cfcb92eb0871b0ae}
---
# txt and pdf carry text; the image has none. The pdf block is nameless and
# gets its name from the <uploaded_files> text block.
id: attachments_with_text_are_hashed_and_named
fixture: frame_attachment
expected:
  - {name: maria.txt, sha256: 576f5772eb89115f882ac39a431b48a4bc1872d84b48573ee1dd5c1f43065055, media_type: text/plain, byte_length: 27}
  - {name: guiaSADT.pdf, sha256: 3f6af091f4b9191b99ff17468d46aee5079b69c13ad754979846b6883e92b5ff, media_type: application/pdf, byte_length: 47}
---
# U_prev holds a Bash result, the fresh round a Read result.
id: only_the_fresh_round_counts
fixture: frame_subagent_child
expected:
  - {name: /home/alice/proj/a.txt, sha256: 49e5ab7eb50b84ab791a464363506fca587318ec5400cfe9d9f08ef43f22103a}
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_event_envelope.py -v`. Expected: collection ERROR, `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `event_envelope.py` (this task's half; the event builders come in 3.3):

```python
"""Frame → AIInvocationObservedV1, and the file-hash set the verdict shares.

Everything here follows the shared forwarders' convention: ``input`` is
the whole transcript the model received, ``output`` is the response, and
``used_tools`` / ``accessed_files`` belong to the round the model
consumed. See the spec's "One transcript walk, two jobs".
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from datetime import UTC, datetime

from slashid_ai_forwarder_core.content_utils import truncate_middle
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.anthropic.normalize import translate_request_messages
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicRequestMessage,
    AnthropicTextBlock,
    AnthropicToolUseBlock,
)
from slashid_ai_forwarder_core.normalize.normalized.tool_results import extract_tool_result_files
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant

from .config import Config
from .frame import Split

PARSED_AS = "anthropic-inference-hook"

# claude.ai lists uploads as ``<file_path>/mnt/user-data/uploads/<name></file_path>``
# in a text block ahead of the attachment blocks. The order does not match
# the blocks, so names are paired by media type.
_UPLOAD_PATH = re.compile(r"<file_path>(.*?)</file_path>")


def _uploaded_names(messages: list[AnthropicRequestMessage]) -> list[str]:
    names: list[str] = []
    for msg in messages:
        for block in msg.content:
            if isinstance(block, AnthropicTextBlock) and "<uploaded_files>" in block.text:
                names.extend(p.rsplit("/", 1)[-1] for p in _UPLOAD_PATH.findall(block.text))
    return names


def _name_for(block: AnthropicAttachmentBlock, candidates: list[str]) -> str | None:
    if block.file_name:
        if block.file_name in candidates:
            candidates.remove(block.file_name)
        return block.file_name
    for i, name in enumerate(candidates):
        guessed, _ = mimetypes.guess_type(name)
        if guessed == block.media_type:
            return candidates.pop(i)
    return None


def attachment_files(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> list[AIAccessedFile]:
    """One AIAccessedFile per text-bearing attachment in the fresh round."""
    fresh = list(after_last_assistant(messages))
    candidates = _uploaded_names(fresh)
    out: list[AIAccessedFile] = []
    for msg in fresh:
        for block in msg.content:
            if not isinstance(block, AnthropicAttachmentBlock) or block.text is None:
                continue
            data = block.text.encode()
            out.append(
                AIAccessedFile(
                    name=_name_for(block, candidates),
                    content_hashes={
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "sha1": hashlib.sha1(data).hexdigest(),
                        "md5": hashlib.md5(data).hexdigest(),
                    },
                    media_type=block.media_type,
                    byte_length=len(data),
                    redacted_content=(
                        truncate_middle(block.text, config.max_content_size)
                        if config.include_raw_content
                        else None
                    ),
                )
            )
    return out


def accessed_files_for(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> list[AIAccessedFile]:
    """Files whose contents the fresh round would put in front of the model.

    Handed the full transcript: the shared extractor builds its tool_use
    index from the assistant messages and selects the fresh round itself.
    """
    return attachment_files(messages, config=config) + extract_tool_result_files(
        translate_request_messages(messages), config=config
    )


def content_request_id(split: Split, session_id: str | None) -> str:
    """Content-addressed id for the invocation whose output is ``split.assistant``.

    The model-minted ``tool_use.id`` when there is one; otherwise a prefixed
    digest of session, assistant ordinal and text. Every re-reveal of the
    same turn converges on the same value, so the server's terminal dedup
    drops duplicates without any state here.
    """
    for msg in split.assistant:
        for block in msg.content:
            if isinstance(block, AnthropicToolUseBlock):
                return block.id
    ordinal = sum(1 for m in split.before if m.role == "assistant")
    text = "".join(
        b.text for m in split.assistant for b in m.content if isinstance(b, AnthropicTextBlock)
    )
    digest = hashlib.sha256(json.dumps([session_id or "", ordinal, text]).encode()).hexdigest()
    return f"hook:{digest[:32]}"


def signed_at_iso(signed_at: int) -> str:
    return datetime.fromtimestamp(signed_at, tz=UTC).isoformat()
```

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_event_envelope.py -v && uv run ty check`. Expected: 8 pass (5 yaml cases + 3) and ty is clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/event_envelope.py anthropic/tests/test_event_envelope.py anthropic/tests/test_accessed_files_for.yaml
git commit -m "feat(anthropic): accessed-files recipe shared by verdict and event paths"
```

### Task 3.3: `previous_invocation_event` and `denial_event`

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/event_envelope.py`
- Test: `anthropic/tests/test_event_envelope.py`, `anthropic/tests/test_previous_invocation_event.yaml`, `anthropic/tests/test_denial_event.yaml`

- [ ] **Step 1: Write the failing tests** — extend the `event_envelope` import to add `PARSED_AS`, `denial_event`, `previous_invocation_event`, and the `slashid_ai_forwarder_core.events` import to add `AIInvocationObservedV1` (unused at 3.2, so `ruff --fix` removed it there), then append:

```python
# --- previous_invocation_event ------------------------------------------------


class ExpectedEvent(BaseModel):
    request_id: str | None = None
    request_id_prefix: str | None = None
    stop_reason: str
    used_tool_ids: list[str] = []
    accessed_files: list[ExpectedFile] = []
    tool_names: set[str] = set()
    servers: set[tuple[str, str]] | None = None


def build(fixture: str, append: list[dict[str, Any]], insert_at: int | None) -> PromptFrame:
    frame = load(fixture)
    extra = [AnthropicRequestMessage.model_validate(m) for m in append]
    messages = list(frame.messages)
    at = len(messages) if insert_at is None else insert_at
    return frame.model_copy(update={"messages": messages[:at] + extra + messages[at:]})


def check_event(event: AIInvocationObservedV1, expected: ExpectedEvent) -> None:
    if expected.request_id is not None:
        assert event.request_id == expected.request_id
    if expected.request_id_prefix is not None:
        assert event.request_id.startswith(expected.request_id_prefix)
        assert len(event.request_id) == len(expected.request_id_prefix) + 32
    assert event.stop_reason == expected.stop_reason
    assert [u.tool_use_id for u in event.used_tools or []] == expected.used_tool_ids
    check_files(event.accessed_files, expected.accessed_files)
    assert {t.name for t in event.available_tools or []} == expected.tool_names
    if expected.servers is not None:
        assert {(s.name, s.kind) for s in event.available_tool_servers or []} == expected.servers


@yaml_pytest(filename="test_previous_invocation_event.yaml")
async def test_previous_invocation_event(
    fixture: str,
    insert_at: int | None,
    append: list[dict[str, Any]],
    expected: ExpectedEvent | None,
) -> None:
    frame = build(fixture, append, insert_at)
    event = await previous_invocation_event(frame, signed_at=SIGNED_AT, config=config())
    if expected is None:
        assert event is None
        return
    assert event is not None
    check_event(event, expected)
    assert event.parsed_as == PARSED_AS
    assert event.conversation_id == frame.session_id
    assert event.input is not None and event.input.content_hashes is not None
    assert event.output is not None and event.output.content_hashes is not None


async def test_envelope_fields_come_from_the_frame() -> None:
    frame = load("frame_tool_result")
    event = await previous_invocation_event(frame, signed_at=SIGNED_AT, config=config())
    assert event is not None
    assert event.timestamp == "2026-09-20T23:08:20+00:00"
    assert event.identity_details.model_dump(exclude_none=True) == {
        "kind": "anthropic",
        "user_id": frame.actor.id,
    }
    assert event.model.id == "claude-opus-5" and event.model.provider == "anthropic"
    assert event.model.raw_model_id == "claude-opus-5"
    assert event.user_agent == "claude-code"
    assert event.tokens.input == 0


async def test_event_hashes_agree_with_the_verdict_path() -> None:
    frame = load("frame_tool_result")
    verdict_files = accessed_files_for(frame.messages, config=config())
    later = build("frame_tool_result", [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}], None)
    event = await previous_invocation_event(later, signed_at=SIGNED_AT, config=config())
    assert event is not None and event.accessed_files is not None
    assert event.accessed_files[0].content_hashes == verdict_files[0].content_hashes


async def test_null_model_becomes_unknown() -> None:
    frame = load("frame_tool_result").model_copy(update={"model": None})
    event = await previous_invocation_event(frame, signed_at=SIGNED_AT, config=config())
    assert event is not None and event.model.id == "unknown" and event.model.raw_model_id is None


async def test_null_actor_id_drops_the_event() -> None:
    frame = load("frame_tool_result")
    frame.actor.id = None
    assert await previous_invocation_event(frame, signed_at=SIGNED_AT, config=config()) is None


# --- denial_event -----------------------------------------------------------------


@yaml_pytest(filename="test_denial_event.yaml")
async def test_denial_event(fixture: str, expected: ExpectedEvent) -> None:
    frame = load(fixture)
    event = await denial_event(frame, signed_at=SIGNED_AT, config=config())
    assert event is not None
    check_event(event, expected)
    assert event.request_id == frame.request_id
    assert event.output is None
    assert event.input is not None
    assert event.conversation_id == frame.session_id


async def test_denial_with_null_actor_is_dropped() -> None:
    frame = load("frame_attachment")
    frame.actor.id = None
    assert await denial_event(frame, signed_at=SIGNED_AT, config=config()) is None
```

with `tests/test_previous_invocation_event.yaml`:

```yaml
id: first_frame_emits_nothing
fixture: frame_first_turn
insert_at: null
append: []
expected: null
---
# Consumption attribution: U_prev is the opening prompt, so nothing was
# consumed yet. The Read result in U_new belongs to the next invocation.
id: tool_result_frame_emits_the_previous_invocation
fixture: frame_tool_result
insert_at: null
append: []
expected:
  request_id: toolu_01Dqhr2d1w2UCUqbXhCSGutC
  stop_reason: tool_use
  used_tool_ids: []
  accessed_files: []
  tool_names: [Read]
---
# U_prev carries the Bash result; Bash is not a read tool, so no file.
# Tools come from every tool_use in the transcript, the response included.
id: consumed_round_drives_used_tools
fixture: frame_subagent_child
insert_at: null
append: []
expected:
  request_id: toolu_01UbdhcQRwkxR8JFoAGZi2i9
  stop_reason: tool_use
  used_tool_ids: [toolu_01SpA8HaR3QvRatgbe8q11GT]
  accessed_files: []
  tool_names: [Bash, Read]
---
# U_prev is two user messages; the ToolSearch result in the first is consumed.
id: mcp_tool_names_yield_their_server
fixture: frame_mcp_tool
insert_at: null
append: []
expected:
  request_id: toolu_01YPqDRNnctTJwvCvzBR7GEs
  stop_reason: tool_use
  used_tool_ids: [toolu_013a8rkKEU8XmKMJNmwAkcXn]
  accessed_files: []
  tool_names: [ToolSearch, echo]
  servers: [[builtin, runtime], [demo, mcp]]
---
# One round later: the model answered after reading, so U_prev is the Read
# result and the file lands on this invocation. A text-only response falls
# back to the prefixed content address.
id: consumed_read_lands_on_accessed_files
fixture: frame_tool_result
insert_at: null
append:
  - {role: assistant, content: [{type: text, text: The first line is SCENARIO-B notes.}]}
expected:
  request_id_prefix: "hook:"
  stop_reason: end_turn
  used_tool_ids: [toolu_01Dqhr2d1w2UCUqbXhCSGutC]
  accessed_files:
    - {name: /home/alice/proj/notes.txt, sha256: e6bab19e50f90145e62b963a3584ec72ea6b88adcb71de6522301ebb9fa0813d}
  tool_names: [Read]
---
# Two consecutive assistant messages are one response; the run now ends in text.
id: consecutive_assistant_messages_are_one_response
fixture: frame_tool_result
insert_at: 2
append:
  - {role: assistant, content: [{type: text, text: and then}]}
expected:
  request_id: toolu_01Dqhr2d1w2UCUqbXhCSGutC
  stop_reason: end_turn
  used_tool_ids: []
  accessed_files: []
  tool_names: [Read]
```

and `tests/test_denial_event.yaml`:

```yaml
# No assistant turn at all: the record is built from the frame alone.
id: attachment_frame
fixture: frame_attachment
expected:
  request_id: chatcompl_011CfFY1XX5Q391ncpUjHeDE
  stop_reason: guardrail_intervened
  used_tool_ids: []
  accessed_files:
    - {name: maria.txt, sha256: 576f5772eb89115f882ac39a431b48a4bc1872d84b48573ee1dd5c1f43065055}
    - {name: guiaSADT.pdf, sha256: 3f6af091f4b9191b99ff17468d46aee5079b69c13ad754979846b6883e92b5ff}
  tool_names: []
---
# The fresh round's Read result is what would have reached the model.
id: tool_result_frame
fixture: frame_tool_result
expected:
  request_id: msg_011CfFXrZo19wubUcJjnSJa9
  stop_reason: guardrail_intervened
  used_tool_ids: [toolu_01Dqhr2d1w2UCUqbXhCSGutC]
  accessed_files:
    - {name: /home/alice/proj/notes.txt, sha256: e6bab19e50f90145e62b963a3584ec72ea6b88adcb71de6522301ebb9fa0813d}
  tool_names: [Read]
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_event_envelope.py -v`. Expected: collection ERROR, `ImportError` on the three new names.

- [ ] **Step 3: Implement** — append to `event_envelope.py`:

```python
def _merge_assistant_run(run: list[AnthropicRequestMessage]) -> AnthropicMessage:
    """A run of consecutive assistant messages is one response."""
    blocks = [
        b
        for m in run
        for b in m.content
        if isinstance(b, AnthropicTextBlock | AnthropicToolUseBlock | AnthropicThinkingBlock | AnthropicUnknownBlock)
    ]
    ends_in_tool_use = bool(blocks) and isinstance(blocks[-1], AnthropicToolUseBlock)
    return AnthropicMessage(
        type="message",
        role="assistant",
        content=blocks,
        stop_reason="tool_use" if ends_in_tool_use else "end_turn",
    )


def _tool_names(blocks: Iterable[object]) -> list[str]:
    """Distinct tool names in first-seen order, across the transcript and the response."""
    seen: dict[str, None] = {}
    for block in blocks:
        if isinstance(block, AnthropicToolUseBlock):
            seen.setdefault(block.name)
    return list(seen)


async def _build(
    frame: PromptFrame,
    *,
    request_messages: list[AnthropicRequestMessage],
    response: AnthropicMessage,
    request_id: str,
    signed_at: int,
    config: Config,
) -> AIInvocationObservedV1 | None:
    if not frame.actor.id:
        # The server rejects an Anthropic identity with no identifier as a
        # permanent error; there is nothing useful to emit.
        return None
    normalized = await message_to_normalized_invocation(
        AnthropicRequestBody(messages=request_messages), response, config=config
    )
    # The frame carries no tool definitions; synthesize them from the names
    # seen, or _used_tools cannot map a result to a tool id.
    blocks = [b for m in request_messages for b in m.content] + list(response.content)
    tools, servers = build_tools_declared((n, None, None) for n in _tool_names(blocks))
    normalized.input.tools_declared = tools
    normalized.input.tool_servers = servers
    normalized.accessed_files = attachment_files(request_messages, config=config)
    finalize(normalized, config=config)
    envelope = EventEnvelope(
        request_id=request_id,
        timestamp=signed_at_iso(signed_at),
        identity_details=AnthropicIdentityDetails(user_id=frame.actor.id),
        model=AIModel(id=frame.model or "unknown", provider="anthropic", raw_model_id=frame.model),
        parsed_as=PARSED_AS,
        user_agent=frame.source.application,
    )
    event = await build_event_from_normalized(normalized, envelope, config=config)
    return event.model_copy(update={"conversation_id": frame.session_id})


async def previous_invocation_event(
    frame: PromptFrame, *, signed_at: int, config: Config
) -> AIInvocationObservedV1 | None:
    """Invocation N, reconstructed from frame N+1. ``None`` on the first frame."""
    split = split_transcript(frame.messages)
    if not split.assistant:
        return None
    return await _build(
        frame,
        request_messages=split.before,
        response=_merge_assistant_run(split.assistant),
        request_id=content_request_id(split, frame.session_id),
        signed_at=signed_at,
        config=config,
    )


async def denial_event(
    frame: PromptFrame, *, signed_at: int, config: Config
) -> AIInvocationObservedV1 | None:
    """The record of an enforced denial, from frame N alone: what the model
    would have seen, no output, and the frame's own request id."""
    event = await _build(
        frame,
        request_messages=frame.messages,
        response=AnthropicMessage(type="message", role="assistant", content=[], stop_reason=None),
        request_id=frame.request_id,
        signed_at=signed_at,
        config=config,
    )
    if event is None:
        return None
    # NormalizedInvocationOutput has no absent state: an empty response still
    # dumps {"stop_reason": "unknown"}, which the builder hashes. Clear it here.
    return event.model_copy(update={"output": None, "stop_reason": "guardrail_intervened"})
```

Merge these into the module's existing import block (ruff's `I` rule sorts them; one `from .frame import ...` line, `Iterable` from `collections.abc`):

```python
from collections.abc import Iterable

from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
    EventEnvelope,
    build_event_from_normalized,
)
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    message_to_normalized_invocation,
    translate_request_messages,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicRequestMessage,
    AnthropicTextBlock,
    AnthropicThinkingBlock,
    AnthropicToolUseBlock,
    AnthropicUnknownBlock,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared
from .frame import PromptFrame, Split, split_transcript
```

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_event_envelope.py -v && uv run ty check`. Expected: 21 pass — 8 from 3.2, 6 yaml cases, 4 plain, 2 denial cases and 1 plain (`ruff format` wraps the long lines, `--fix` sorts the merged imports). If `ty` complains that `_WireModel` fields are not assignable in `model_copy(update=...)`, that is a false positive on pydantic's signature; `model_copy` is the intended API. If `AnthropicMessage(content=blocks)` fails validation because a block class is not in the response union, the `isinstance` filter in `_merge_assistant_run` is wrong — fix the filter, not the schema.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/event_envelope.py anthropic/tests/test_event_envelope.py anthropic/tests/test_previous_invocation_event.yaml anthropic/tests/test_denial_event.yaml
git commit -m "feat(anthropic): emit-previous reconstruction and the enforced-denial record"
```

---

## Chunk 4: Verdict — policy, preflight, composition

### Task 4.1: `checks.py` — shared verdict types

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/checks.py`

No behaviour of its own; created with the first client in 4.2. Contents:

```python
"""Types shared by the two verdict checks and their composer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


class CheckFailed(Exception):
    """The check could not answer: transport failure, non-200, malformed body.
    The composer applies the configured fail mode."""


@dataclass(frozen=True)
class Verdict:
    action: Literal["allow", "deny"]
    deny_reason: str | None = None
    reference_id: str | None = None
    # Which check decided: "policy", "preflight", "marker", "fail_mode", "bypass",
    # "observe" (enforce off), "none".
    source: str = "none"

    @property
    def denied(self) -> bool:
        return self.action == "deny"

    def to_wire(self) -> dict[str, str]:
        wire: dict[str, str] = {"action": self.action}
        if self.denied:
            if self.deny_reason:
                wire["deny_reason"] = self.deny_reason[:500]
            if self.reference_id:
                wire["reference_id"] = self.reference_id
        return wire


ALLOW = Verdict("allow")
```

### Task 4.2: `policy.py` — forward the raw frame to the Go receiver

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/policy.py`, `checks.py` (above)
- Test: `anthropic/tests/test_policy.py`

- [ ] **Step 1: Write the failing tests**

```python
"""The policy receiver re-verifies the signature, so the forward must be
byte-for-byte the frame Anthropic sent, with its three webhook headers."""

from __future__ import annotations

import httpx
import pytest

from slashid_anthropic_forwarder.checks import CheckFailed
from slashid_anthropic_forwarder.policy import policy_check

URL = "https://api.slashid.example/ai-access/acme"
HEADERS = {
    "Webhook-Id": "msg_1",
    "webhook-timestamp": "1789945700",
    "webhook-signature": "v1,abc",
    "content-type": "application/json",
    "user-agent": "anthropic-dlp/1",
    "x-forwarded-for": "1.2.3.4",
}
BODY = b'{"type":"prompt","request_id":"msg_1"}'


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_forwards_raw_bytes_and_only_the_webhook_headers() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"action": "allow"})

    async with client(handler) as c:
        verdict = await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)
    assert verdict.action == "allow" and verdict.source == "policy"
    assert seen["body"] == BODY
    assert seen["headers"]["webhook-id"] == "msg_1"
    assert seen["headers"]["webhook-timestamp"] == "1789945700"
    assert seen["headers"]["webhook-signature"] == "v1,abc"
    assert seen["headers"]["content-type"] == "application/json"
    assert "x-forwarded-for" not in seen["headers"]
    assert "content-encoding" not in seen["headers"]


async def test_deny_with_reason_and_reference_is_returned() -> None:
    async with client(lambda r: httpx.Response(200, json={"action": "deny", "deny_reason": "no", "reference_id": "ref"})) as c:
        verdict = await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)
    assert verdict.action == "deny" and verdict.deny_reason == "no" and verdict.reference_id == "ref"


@pytest.mark.parametrize("status", [401, 404, 500])
async def test_non_200_raises(status: int) -> None:
    async with client(lambda r: httpx.Response(status, text="x")) as c:
        with pytest.raises(CheckFailed):
            await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)


async def test_unknown_action_raises() -> None:
    async with client(lambda r: httpx.Response(200, json={"action": "maybe"})) as c:
        with pytest.raises(CheckFailed):
            await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)


async def test_transport_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with client(handler) as c:
        with pytest.raises(CheckFailed):
            await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_policy.py -v`. Expected: collection ERROR, `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `policy.py`:

```python
"""Client for the Go policy receiver (``POST /ai-access/<id>``).

It verifies Anthropic's signature itself, so the frame is forwarded as the
raw bytes received, with the three ``webhook-*`` headers copied verbatim
and nothing else. Re-serializing or compressing would break its check.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from .checks import CheckFailed, Verdict

_FORWARDED = ("webhook-id", "webhook-timestamp", "webhook-signature")


async def policy_check(
    client: httpx.AsyncClient,
    *,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout_s: float,
) -> Verdict:
    lower = {k.lower(): v for k, v in headers.items()}
    forward = {k: lower[k] for k in _FORWARDED if k in lower}
    forward["content-type"] = "application/json"
    try:
        response = await client.post(url, content=body, headers=forward, timeout=timeout_s)
    except httpx.HTTPError as exc:
        raise CheckFailed(f"policy: {exc!r}") from exc
    if response.status_code != 200:
        raise CheckFailed(f"policy: HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CheckFailed("policy: unparseable verdict") from exc
    action = data.get("action") if isinstance(data, dict) else None
    if action not in ("allow", "deny"):
        raise CheckFailed(f"policy: unknown action {action!r}")
    return Verdict(
        action=action,
        deny_reason=data.get("deny_reason") or None,
        reference_id=data.get("reference_id") or None,
        source="policy",
    )
```

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_policy.py -v && uv run ruff check .`. Expected: 7 pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/checks.py anthropic/src/slashid_anthropic_forwarder/policy.py anthropic/tests/test_policy.py
git commit -m "feat(anthropic): policy-receiver client forwarding the raw signed frame"
```

### Task 4.3: `preflight.py` — content check

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/preflight.py`
- Test: `anthropic/tests/test_preflight.py`

- [ ] **Step 1: Write the failing tests**

```python
"""POST /ip/nhi/ai/preflight per ng-evangelion PR #7733: one verdict per check
plus ``overall``; ``verified: false`` means "apply your own fail mode"."""

from __future__ import annotations

import json

import httpx
import pytest
from slashid_ai_forwarder_core.events import AIAccessedFile

from slashid_anthropic_forwarder.checks import CheckFailed
from slashid_anthropic_forwarder.preflight import preflight_check

ENDPOINT = "https://api.slashid.example"
IDENTITY = {"kind": "anthropic", "user_id": "user_01A"}
FILES = [
    AIAccessedFile(name="a.txt", content_hashes={"sha256": "aa", "sha1": "bb", "md5": "cc"}, media_type="text/plain", byte_length=3, redacted_content="secret"),
    AIAccessedFile(name="b.txt", content_hashes={"sha256": "dd"}),
]


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(overall: dict, **rest) -> httpx.Response:
    return httpx.Response(200, json={"overall": overall, **rest})


async def test_request_shape_and_auth() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return ok({"allowed": True, "verified": True})

    async with client(handler) as c:
        await preflight_check(c, endpoint=ENDPOINT, push_token="tok", identity=IDENTITY, model="claude-opus-5", files=FILES, timeout_s=1.0)
    assert seen["url"] == f"{ENDPOINT}/ip/nhi/ai/preflight"
    assert seen["auth"] == "Bearer tok"
    body = seen["json"]
    assert body["identity_details"] == IDENTITY
    assert body["model"] == {"id": "claude-opus-5"}
    assert body["accessed_files"][0] == {"name": "a.txt", "content_hashes": {"sha256": "aa", "sha1": "bb", "md5": "cc"}, "media_type": "text/plain", "byte_length": 3}
    assert "redacted_content" not in body["accessed_files"][0]


async def test_model_omitted_when_null() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok({"allowed": True, "verified": True})

    async with client(handler) as c:
        await preflight_check(c, endpoint=ENDPOINT, push_token="tok", identity=IDENTITY, model=None, files=FILES, timeout_s=1.0)
    assert "model" not in seen["json"]


async def test_verified_allow() -> None:
    async with client(lambda r: ok({"allowed": True, "verified": True})) as c:
        v = await preflight_check(c, endpoint=ENDPOINT, push_token="tok", identity=IDENTITY, model=None, files=FILES, timeout_s=1.0)
    assert v is not None and v.action == "allow" and v.source == "preflight"


async def test_verified_deny_carries_message() -> None:
    async with client(lambda r: ok({"allowed": False, "verified": True, "message": "a.txt is marked sensitive."})) as c:
        v = await preflight_check(c, endpoint=ENDPOINT, push_token="tok", identity=IDENTITY, model=None, files=FILES, timeout_s=1.0)
    assert v is not None and v.action == "deny" and v.deny_reason == "a.txt is marked sensitive."


async def test_unverified_returns_none() -> None:
    async with client(lambda r: ok({"allowed": True, "verified": False})) as c:
        assert await preflight_check(c, endpoint=ENDPOINT, push_token="tok", identity=IDENTITY, model=None, files=FILES, timeout_s=1.0) is None


async def test_over_cap_is_unverified_without_calling() -> None:
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(1)
        return ok({"allowed": True, "verified": True})

    many = [AIAccessedFile(name=f"{i}.txt", content_hashes={"sha256": "x"}) for i in range(101)]
    async with client(handler) as c:
        assert await preflight_check(c, endpoint=ENDPOINT, push_token="tok", identity=IDENTITY, model=None, files=many, timeout_s=1.0) is None
    assert called == []


@pytest.mark.parametrize("status", [400, 401, 404, 503])
async def test_non_200_raises(status: int) -> None:
    async with client(lambda r: httpx.Response(status, text="x")) as c:
        with pytest.raises(CheckFailed):
            await preflight_check(c, endpoint=ENDPOINT, push_token="tok", identity=IDENTITY, model=None, files=FILES, timeout_s=1.0)
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_preflight.py -v`. Expected: collection ERROR, `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `preflight.py`:

```python
"""Client for ``POST /ip/nhi/ai/preflight`` (ng-evangelion PR #7733).

Only ``overall`` is read. ``verified: false`` is returned as ``None`` so the
composer applies the fail mode; a transport failure raises ``CheckFailed``
for the same treatment. Neither is ever turned into an allow here.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from slashid_ai_forwarder_core.events import AIAccessedFile

from .checks import CheckFailed, Verdict

log = logging.getLogger(__name__)

MAX_FILES = 100
MAX_BODY_BYTES = 1024 * 1024


def _entry(f: AIAccessedFile) -> dict[str, Any]:
    # Never redacted_content: the endpoint ignores it and the body cap counts it.
    return f.model_dump(exclude_none=True, exclude={"redacted_content"})


async def preflight_check(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    push_token: str,
    identity: dict[str, Any],
    model: str | None,
    files: list[AIAccessedFile],
    timeout_s: float,
) -> Verdict | None:
    if len(files) > MAX_FILES:
        log.warning("preflight: %d files exceed the %d cap; unverified", len(files), MAX_FILES)
        return None
    body: dict[str, Any] = {
        "identity_details": identity,
        "accessed_files": [_entry(f) for f in files],
    }
    if model:
        body["model"] = {"id": model}
    request = client.build_request(
        "POST",
        f"{endpoint}/ip/nhi/ai/preflight",
        json=body,
        headers={"Authorization": f"Bearer {push_token}"},
        timeout=timeout_s,
    )
    if len(request.content) > MAX_BODY_BYTES:
        log.warning("preflight: body %d bytes exceeds the cap; unverified", len(request.content))
        return None
    try:
        response = await client.send(request)
    except httpx.HTTPError as exc:
        raise CheckFailed(f"preflight: {exc!r}") from exc
    if response.status_code != 200:
        raise CheckFailed(f"preflight: HTTP {response.status_code}")
    try:
        overall = response.json()["overall"]
        allowed, verified = bool(overall["allowed"]), bool(overall["verified"])
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckFailed("preflight: unparseable verdict") from exc
    if not verified:
        return None
    if not allowed:
        return Verdict("deny", deny_reason=overall.get("message") or None, source="preflight")
    return Verdict("allow", source="preflight")
```

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_preflight.py -v && uv run ruff check .`. Expected: 10 pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/preflight.py anthropic/tests/test_preflight.py
git commit -m "feat(anthropic): preflight client"
```

### Task 4.4: `verdict.py` — concurrency, budget, fail mode, enforce

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/verdict.py`
- Test: `anthropic/tests/test_verdict.py`, `anthropic/tests/test_decide.yaml`

Unknown top-level `type` is checked twice on purpose: `main.py` answers allow before it ever calls `decide`, and `decide` answers `bypass` for it as well, because `decide` is also called directly by these tests and must be safe on its own. Do not remove either.

- [ ] **Step 1: Write the failing tests**

```python
"""Composition: any deny denies; a check that cannot answer takes the fail
mode; config-test and unknown types bypass; observe-only always allows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
from typing import Any

import httpx
from pydantic import BaseModel
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.checks import Verdict
from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.frame import PromptFrame
from slashid_anthropic_forwarder.verdict import decide, reference_id
from tests.conftest import SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
POLICY = "https://policy.example/ai-access/acme"
FILES = [AIAccessedFile(name="a.txt", content_hashes={"sha256": "x"})]
HEADERS = {"webhook-id": "msg_1", "webhook-timestamp": "1", "webhook-signature": "v1,a"}


class Mock(BaseModel):
    """How a check's endpoint answers: a status + JSON body, a transport
    error, or a sleep long enough to blow the budget."""

    status: int = 200
    body: dict[str, Any] | None = None
    error: bool = False
    sleep: float = 0.0


class Expected(BaseModel):
    action: str
    source: str
    calls: list[str] | None = None  # sorted; None = don't care


def config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.example",
        "push_token": "t",
        "hook_signing_secret": SECRET,
        "enforce": True,
        "policy_url": POLICY,
    }
    base.update(overrides)
    return Config(**base)


def frame(update: dict[str, Any]) -> PromptFrame:
    raw = json.loads((FIXTURES / "frame_tool_result.json").read_text())
    return PromptFrame.model_validate({**raw, **update})


def router(policy: Mock | None, preflight: Mock | None):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        which = "policy" if request.url.host == "policy.example" else "preflight"
        calls.append(which)
        spec = policy if which == "policy" else preflight
        assert spec is not None, f"{which} was called but the case gave it no answer"
        if spec.sleep:
            await asyncio.sleep(spec.sleep)
        if spec.error:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(spec.status, json=spec.body)

    return handler, calls


async def run(cfg: Config, handler, *, files: bool, body: bytes, fr: PromptFrame) -> Verdict:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await decide(
            fr, raw_body=body, headers=HEADERS, files=FILES if files else [], config=cfg, client=c
        )


@yaml_pytest(filename="test_decide.yaml")
async def test_decide(
    policy: Mock | None,
    preflight: Mock | None,
    config_overrides: dict[str, Any],
    frame_update: dict[str, Any],
    files: bool,
    body: str,
    expected: Expected,
) -> None:
    handler, calls = router(policy, preflight)
    verdict = await run(
        config(**config_overrides), handler, files=files, body=body.encode(), fr=frame(frame_update)
    )
    assert (verdict.action, verdict.source) == (expected.action, expected.source)
    if verdict.denied:
        assert verdict.reference_id == reference_id("msg_1")
    if expected.calls is not None:
        assert sorted(calls) == expected.calls


async def test_deny_reason_is_capped_at_500_chars() -> None:
    handler, _ = router(Mock(body={"action": "deny", "deny_reason": "x" * 900}), Mock(body={"overall": {"allowed": True, "verified": True}}))
    verdict = await run(config(), handler, files=True, body=b"{}", fr=frame({}))
    assert verdict.deny_reason.startswith("x" * 900)  # capped only on the wire
    assert verdict.deny_reason.endswith("Start a new conversation to continue.")
    assert verdict.to_wire()["deny_reason"] == "x" * 500


def test_reference_id_matches_the_go_recipe() -> None:
    assert reference_id("msg_1") == hashlib.sha256(b"msg_1").hexdigest()[:32]
```

with `tests/test_decide.yaml` (every case names every parameter; `body: "{}"` is a frame body without the marker):

```yaml
id: both_allow
policy: {body: {action: allow}}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {}
frame_update: {}
files: true
body: "{}"
expected: {action: allow, source: none, calls: [policy, preflight]}
---
id: policy_deny_wins_with_its_reason
policy: {body: {action: deny, deny_reason: Not in hours.}}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {}
frame_update: {}
files: true
body: "{}"
expected: {action: deny, source: policy}
---
id: preflight_deny_wins_with_its_message
policy: {body: {action: allow}}
preflight: {body: {overall: {allowed: false, verified: true, message: a.txt is marked sensitive.}}}
config_overrides: {}
frame_update: {}
files: true
body: "{}"
expected: {action: deny, source: preflight}
---
id: policy_transport_failure_fails_open_by_default
policy: {status: 503}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {}
frame_update: {}
files: true
body: "{}"
expected: {action: allow, source: none}
---
id: policy_transport_failure_fails_closed_when_configured
policy: {status: 503}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {verdict_fail_mode: deny}
frame_update: {}
files: true
body: "{}"
expected: {action: deny, source: fail_mode}
---
id: policy_timeout_takes_fail_mode
policy: {error: true}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {verdict_fail_mode: deny}
frame_update: {}
files: true
body: "{}"
expected: {action: deny, source: fail_mode}
---
id: unverified_preflight_fails_open_by_default
policy: {body: {action: allow}}
preflight: {body: {overall: {allowed: true, verified: false}}}
config_overrides: {}
frame_update: {}
files: true
body: "{}"
expected: {action: allow, source: none}
---
id: unverified_preflight_fails_closed_when_configured
policy: {body: {action: allow}}
preflight: {body: {overall: {allowed: true, verified: false}}}
config_overrides: {verdict_fail_mode: deny}
frame_update: {}
files: true
body: "{}"
expected: {action: deny, source: fail_mode}
---
# The budget cancels the in-flight call, so this case runs in about 100 ms.
id: budget_exceeded_takes_fail_mode
policy: {sleep: 2.0, body: {action: allow}}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {verdict_budget_ms: 100, verdict_fail_mode: deny}
frame_update: {}
files: true
body: "{}"
expected: {action: deny, source: fail_mode}
---
id: disabled_checks_are_skipped_not_unverified
policy: null
preflight: null
config_overrides: {policy_url: null, preflight_enabled: false, verdict_fail_mode: deny}
frame_update: {}
files: true
body: "{}"
expected: {action: allow, source: none, calls: []}
---
id: preflight_not_called_without_files
policy: {body: {action: allow}}
preflight: null
config_overrides: {verdict_fail_mode: deny}
frame_update: {}
files: false
body: "{}"
expected: {action: allow, source: none, calls: [policy]}
---
id: preflight_not_called_without_actor_id
policy: {body: {action: allow}}
preflight: null
config_overrides: {verdict_fail_mode: deny}
frame_update: {actor: {type: user, id: null, email_address: null}}
files: true
body: "{}"
expected: {action: allow, source: none, calls: [policy]}
---
# The policy receiver would deny both; the protocol wants allow for both.
id: config_test_bypasses_both_checks
policy: {body: {action: deny, deny_reason: "no"}}
preflight: {body: {overall: {allowed: false, verified: true}}}
config_overrides: {}
frame_update: {source: {application: config-test}}
files: true
body: "{}"
expected: {action: allow, source: bypass, calls: []}
---
id: unknown_type_bypasses_both_checks
policy: {body: {action: deny, deny_reason: "no"}}
preflight: {body: {overall: {allowed: false, verified: true}}}
config_overrides: {}
frame_update: {type: response}
files: true
body: "{}"
expected: {action: allow, source: bypass, calls: []}
---
id: marker_denies_under_enforce
policy: {body: {action: allow}}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {capture_deny_marker: SLASHID_DENY_ME}
frame_update: {}
files: true
body: '{"x": "SLASHID_DENY_ME"}'
expected: {action: deny, source: marker}
---
# Observe-only still runs every check, then answers allow.
id: observe_only_runs_checks_but_allows
policy: {body: {action: deny, deny_reason: "no"}}
preflight: {body: {overall: {allowed: false, verified: true}}}
config_overrides: {enforce: false}
frame_update: {}
files: true
body: "{}"
expected: {action: allow, source: observe, calls: [policy, preflight]}
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_verdict.py -v`. Expected: collection ERROR, `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `verdict.py`:

```python
"""Compose the verdict: two optional checks, concurrently, under one budget.

Owns rule 2 of the design: what to answer when a check cannot. Any deny
denies; a failed or unverified check takes ``verdict_fail_mode``; a
disabled check is simply absent. Observe-only evaluates and logs, then
answers allow.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Mapping

import httpx
from slashid_ai_forwarder_core.events import AIAccessedFile

from .checks import ALLOW, CheckFailed, Verdict
from .config import Config
from .frame import PromptFrame
from .policy import policy_check
from .preflight import preflight_check

log = logging.getLogger(__name__)


def reference_id(webhook_id: str) -> str:
    """Same recipe as the Go policy receiver, so every record joins on one value."""
    return hashlib.sha256(webhook_id.encode()).hexdigest()[:32]


def _fail_mode(config: Config, why: str) -> Verdict:
    log.warning("verdict: %s; applying fail mode %s", why, config.verdict_fail_mode)
    if config.verdict_fail_mode == "deny":
        return Verdict("deny", deny_reason="Your organization's policy check is unavailable.", source="fail_mode")
    return Verdict("allow", source="fail_mode")


async def _settle(name: str, task: Awaitable[Verdict | None], config: Config) -> Verdict:
    try:
        result = await task
    except CheckFailed as exc:
        return _fail_mode(config, f"{name} failed: {exc}")
    if result is None:
        return _fail_mode(config, f"{name} unverified")
    return result


async def decide(
    frame: PromptFrame,
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    files: list[AIAccessedFile],
    config: Config,
    client: httpx.AsyncClient,
) -> Verdict:
    lower = {k.lower(): v for k, v in headers.items()}
    webhook_id = lower.get("webhook-id", frame.request_id)
    ref = reference_id(webhook_id)

    if frame.type != "prompt" or frame.is_connection_test():
        # The policy receiver denies both; the protocol wants allow for both.
        return Verdict("allow", source="bypass")

    checks: list[tuple[str, Awaitable[Verdict | None]]] = []
    if config.policy_url:
        checks.append(("policy", policy_check(
            client, url=config.policy_url, body=raw_body, headers=headers,
            timeout_s=config.verdict_budget_ms / 1000,
        )))
    if config.preflight_enabled and files and frame.actor.id:
        # A null actor id cannot be resolved server-side; chunk 3 drops such
        # events, and a 400 here would only engage the fail mode.
        checks.append(("preflight", preflight_check(
            client, endpoint=config.endpoint, push_token=config.push_token,
            identity={"kind": "anthropic", "user_id": frame.actor.id},
            model=frame.model, files=files, timeout_s=config.verdict_budget_ms / 1000,
        )))

    results: list[Verdict] = []
    if config.capture_deny_marker and config.capture_deny_marker.encode() in raw_body:
        results.append(Verdict("deny", deny_reason="Denied by the SlashID capture test marker.", source="marker"))
    if checks:
        try:
            settled = await asyncio.wait_for(
                asyncio.gather(*(_settle(n, t, config) for n, t in checks)),
                timeout=config.verdict_budget_ms / 1000,
            )
            results.extend(settled)
        except TimeoutError:
            results.append(_fail_mode(config, "verdict budget exceeded"))

    # Sticky denials: the offending content stays in the fresh round, so the
    # conversation cannot recover. Anthropic's guidance is to say what to
    # change; the only true answer is to start over.
    RECOVERY = " Start a new conversation to continue."

    composed = next((r for r in results if r.denied), ALLOW)
    if composed.denied:
        reason = (composed.deny_reason or "Blocked by your organization's policy.") + RECOVERY
        composed = Verdict("deny", deny_reason=reason, reference_id=ref, source=composed.source)
    log.info(
        "verdict %s: %s via %s (enforce=%s, checks=%s)",
        webhook_id, composed.action, composed.source, config.enforce, [n for n, _ in checks],
    )
    if not config.enforce:
        return Verdict("allow", source="observe")
    return composed
```

On budget timeout `wait_for` cancels the gathered tasks, and the cancellation propagates into the in-flight httpx call, so the `budget_exceeded_takes_fail_mode` case completes in about the budget, not the handler's 2 s sleep. No extra cancellation handling is needed.

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_verdict.py -v && uv run ruff check . && uv run ty check`. Expected: 18 pass (16 yaml cases + 2), no warnings.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/verdict.py anthropic/tests/test_verdict.py anthropic/tests/test_decide.yaml
git commit -m "feat(anthropic): verdict composition with fail mode and observe-only"
```

---

## Chunk 5: Entrypoint integration and push isolation

### Task 5.1: `main.py` — verdict path, background push, rule 1

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/main.py` (full rewrite of the route)
- Test: `anthropic/tests/test_main.py` (extend; keep the ten existing tests passing)

- [ ] **Step 1: Write the failing tests** — in `tests/test_main.py`, add `asyncio`, `pathlib` and `time` to the import block at the top (`json` is already there; a mid-file import trips ruff `E402`). Change `_client` so no test can ever build a real sink, and rewrite the existing capture test to drain, since the capture now runs in a spawned task rather than a Starlette background task and `ASGITransport` does not wait for spawned tasks:

```python
def _client(config: Config, capture: MemoryCapture | None = None) -> httpx.AsyncClient:
    app = create_app(config, capture=capture, pusher=MemoryPusher())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_frame_is_captured_raw_with_its_headers(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    capture = MemoryCapture()
    app = create_app(_config(capture_bucket="b"), capture=capture, pusher=MemoryPusher())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/", content=body, headers=sign(body, "req_test"))
    await app.state.drain()
    assert len(capture.stored) == 1
    request_id, headers, stored_body = capture.stored[0]
    assert request_id == "req_test"
    assert stored_body == body  # raw bytes, not a re-encoding
    assert headers["webhook-id"] == "req_test"
```

`MemoryPusher` can live anywhere at module level (the appended block below defines it). Then append:

```python
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIXTURES / f"{name}.json").read_bytes()


class MemoryPusher:
    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.events: list = []
        self.fail, self.delay = fail, delay

    async def __call__(self, events: list) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("sink down")
        self.events.extend(events)


async def test_previous_invocation_is_pushed_after_a_tool_result_frame(sign: Signer) -> None:
    body = fixture("frame_tool_result")
    pusher = MemoryPusher()
    app = create_app(_config(policy_url=None, preflight_enabled=False), pusher=pusher)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_011CfFXrZo19wubUcJjnSJa9"))
    assert r.json() == {"action": "allow"}
    await app.state.drain()
    assert [e.request_id for e in pusher.events] == ["toolu_01Dqhr2d1w2UCUqbXhCSGutC"]


async def test_first_frame_pushes_nothing(sign: Signer) -> None:
    body = fixture("frame_first_turn")
    pusher = MemoryPusher()
    app = create_app(_config(preflight_enabled=False), pusher=pusher)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/", content=body, headers=sign(body, "msg_011CfFXr6iaijsKMoru1s9uf"))
    await app.state.drain()
    assert pusher.events == []


async def test_sink_failure_never_reaches_the_verdict(sign: Signer) -> None:
    """Rule 1. A non-200 from us is a webhook failure that hands control to the
    customer's fail-open/fail-closed setting; a sink outage must not do that."""
    body = fixture("frame_tool_result")
    app = create_app(_config(preflight_enabled=False), pusher=MemoryPusher(fail=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_011CfFXrZo19wubUcJjnSJa9"))
    await app.state.drain()
    assert r.status_code == 200 and r.json() == {"action": "allow"}


async def test_slow_sink_does_not_delay_the_response(sign: Signer) -> None:
    body = fixture("frame_tool_result")
    pusher = MemoryPusher(delay=0.5)
    app = create_app(_config(preflight_enabled=False, push_budget_ms=100), pusher=pusher)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        started = time.monotonic()
        r = await c.post("/", content=body, headers=sign(body, "msg_011CfFXrZo19wubUcJjnSJa9"))
        elapsed = time.monotonic() - started
    assert r.status_code == 200 and elapsed < 0.3
    await app.state.drain()
    assert pusher.events == []  # the budget cut it off; logged, not retried here


async def test_enforced_denial_pushes_the_denial_record_and_the_previous_one(sign: Signer) -> None:
    body = fixture("frame_tool_result")
    pusher = MemoryPusher()
    app = create_app(_config(preflight_enabled=False, enforce=True, capture_deny_marker="SCENARIO-B"), pusher=pusher)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_011CfFXrZo19wubUcJjnSJa9"))
    assert r.json()["action"] == "deny"
    await app.state.drain()
    ids = sorted(e.request_id for e in pusher.events)
    assert ids == ["msg_011CfFXrZo19wubUcJjnSJa9", "toolu_01Dqhr2d1w2UCUqbXhCSGutC"]
    denial = next(e for e in pusher.events if e.request_id == "msg_011CfFXrZo19wubUcJjnSJa9")
    assert denial.output is None and denial.stop_reason == "guardrail_intervened"


async def test_observe_only_deny_pushes_no_denial_record(sign: Signer) -> None:
    body = fixture("frame_tool_result")
    pusher = MemoryPusher()
    app = create_app(_config(preflight_enabled=False, capture_deny_marker="SCENARIO-B"), pusher=pusher)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_011CfFXrZo19wubUcJjnSJa9"))
    assert r.json() == {"action": "allow"}
    await app.state.drain()
    assert [e.request_id for e in pusher.events] == ["toolu_01Dqhr2d1w2UCUqbXhCSGutC"]
```

The existing deny-marker test keeps passing: the marker still denies only under `enforce`, now inside `decide`.

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_main.py -v`. Expected: every test in the file fails with `TypeError: create_app() got an unexpected keyword argument 'pusher'`, since `_client` now passes it.

- [ ] **Step 3: Implement** — rewrite `main.py`:

```python
"""FastAPI entrypoint: one POST route at any path, signature gate, verdict.

Owns rule 1 of the design: the push and the capture run in tracked tasks
after the response is written, bounded by ``push_budget_ms``, and nothing
they do can change the response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.sink import push_invocations

from .capture import Capture, GcsCapture
from .checks import ALLOW, Verdict
from .config import Config, load_config
from .event_envelope import accessed_files_for, denial_event, previous_invocation_event
from .frame import PromptFrame
from .signature import verify
from .verdict import decide

log = logging.getLogger(__name__)

Pusher = Callable[[list[AIInvocationObservedV1]], Awaitable[None]]


def _sink_pusher(config: Config, client: httpx.AsyncClient) -> Pusher:
    async def push(events: list[AIInvocationObservedV1]) -> None:
        await push_invocations(
            client, events, endpoint=config.endpoint, push_token=config.push_token,
            max_retries=config.max_retries,
        )

    return push


def create_app(
    config: Config, *, capture: Capture | None = None, pusher: Pusher | None = None
) -> FastAPI:
    client = httpx.AsyncClient(timeout=httpx.Timeout(config.request_timeout_seconds))
    if capture is None and config.capture_bucket:
        capture = GcsCapture(config.capture_bucket)
    push = pusher or _sink_pusher(config, client)
    pending: set[asyncio.Task[None]] = set()

    def spawn(coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def drain() -> None:
        """Await every background task. Tests and shutdown only."""
        while pending:
            await asyncio.gather(*list(pending), return_exceptions=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await drain()
        await client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.drain = drain

    async def emit(frame: PromptFrame, signed_at: int, verdict: Verdict) -> None:
        events: list[AIInvocationObservedV1] = []
        try:
            previous = await previous_invocation_event(frame, signed_at=signed_at, config=config)
            if previous is not None:
                events.append(previous)
            if verdict.denied and config.enforce:
                denial = await denial_event(frame, signed_at=signed_at, config=config)
                if denial is not None:
                    events.append(denial)
            if events:
                await asyncio.wait_for(push(events), timeout=config.push_budget_ms / 1000)
        except Exception:
            log.exception("push failed for %s (%d events dropped)", frame.request_id, len(events))

    async def capture_safely(request_id: str, headers: dict[str, str], body: bytes) -> None:
        try:
            assert capture is not None
            await capture.store(request_id, headers, body)
        except Exception:
            log.exception("capture failed for %s", request_id)

    @app.post("/{path:path}")
    async def hook(request: Request) -> Response:
        body = await request.body()
        if len(body) > config.max_body_bytes:
            return Response(status_code=413)
        headers = dict(request.headers)
        if not verify(config.signing_secrets, headers, body) and not config.hook_allow_unsigned:
            return Response(status_code=401)
        webhook_id = headers.get("webhook-id", "")
        try:
            signed_at = int(headers.get("webhook-timestamp", ""))
        except ValueError:
            signed_at = int(time.time())
        if capture is not None:
            spawn(capture_safely(webhook_id, headers, body))

        try:
            frame = PromptFrame.model_validate(json.loads(body))
        except (ValueError, ValidationError):
            log.warning("frame %s did not parse; allowing", webhook_id)
            return JSONResponse(ALLOW.to_wire())
        if frame.type != "prompt":
            log.warning("frame %s has type %r; allowing", webhook_id, frame.type)
            return JSONResponse(ALLOW.to_wire())

        probe = frame.is_connection_test()
        files = [] if probe else accessed_files_for(frame.messages, config=config)
        verdict = await decide(
            frame, raw_body=body, headers=headers, files=files, config=config, client=client
        )
        if not probe:
            spawn(emit(frame, signed_at, verdict))
        return JSONResponse(verdict.to_wire())

    return app


def app() -> FastAPI:
    """uvicorn factory: ``uvicorn slashid_anthropic_forwarder.main:app --factory``.

    uvicorn configures only its own loggers, so without a root handler our
    INFO lines fall to Python's last-resort handler and are dropped.
    """
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return create_app(load_config())
```

The deny marker moved into `decide`; delete its handling from the old route. `_reference_id` moved to `verdict.reference_id`.

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run pytest -v && uv run ruff check . && uv run ruff format --check . && uv run ty check`. Expected: every test in the subproject passes (16 in `test_main.py`), no deprecation warnings. `ASGITransport` never runs the lifespan, which is why tests call `app.state.drain()` themselves.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/main.py anthropic/tests/test_main.py
git commit -m "feat(anthropic): verdict path and background event push with failure isolation"
```

### Task 5.2: Live re-run against the test tenant

- [ ] **Step 1:** `./anthropic/deploy/dev-deploy.sh strong-hue-507702-k7 us-central1` (preflight stays disabled by the script; enforce defaults to false).
- [ ] **Step 2:** Put a placeholder-free push token for an **Anthropic** connection into `slashid_anthropic_push_token` (the person with the SlashID org does this), then re-run the script.
- [ ] **Step 3:** From a scratch directory, `CLAUDE_CONFIG_DIR=~/.claude-work claude -p "Read notes.txt and reply with its first line." --allowedTools Read`, then `gcloud logging read 'resource.labels.service_name="slashid-anthropic-forwarder" textPayload:"push_invocations"' --limit 5` and confirm one batch was posted, and check the invocation in SlashID.

---

## Chunk 6: Deploy, release, docs, CI

### Task 6.0: Empty `SLASHID_POLICY_URL` means unset

Terraform always sets the variable, as `""` when the policy receiver is not configured; pydantic-settings does not treat `""` as unset for `str | None`.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/config.py`
- Test: `anthropic/tests/test_config.py`

- [ ] **Step 1: Write the failing test** — append:

```python
def test_empty_policy_url_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_POLICY_URL="")
    assert Config().policy_url is None
```

- [ ] **Step 2: Run** — `cd anthropic && uv run pytest tests/test_config.py -k empty_policy_url -v`. Expected: FAIL, `'' is not None`.

- [ ] **Step 3: Implement** — in `Config`, next to the existing validator (import `field_validator` alongside `model_validator`):

```python
    @field_validator("policy_url", mode="before")
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        return v or None
```

- [ ] **Step 4: Run** — `cd anthropic && uv run pytest tests/test_config.py -v`. Expected: pass.

- [ ] **Step 5: Commit** — `git add anthropic/src/slashid_anthropic_forwarder/config.py anthropic/tests/test_config.py && git commit -m "fix(anthropic): empty SLASHID_POLICY_URL means unset"`.

### Task 6.1: Terraform module

**Files:**
- Create: `anthropic/deploy/terraform/{versions,variables,main,secrets,registry,service,iam,outputs}.tf`, `anthropic/deploy/terraform/README.md`

- [ ] **Step 1:** Write the files.

`versions.tf`:

```hcl
terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
  }
}
```

`variables.tf`:

```hcl
variable "project_id" {
  description = "GCP project that hosts the receiver."
  type        = string
}

variable "region" {
  description = "Cloud Run region."
  type        = string
  default     = "us-central1"
}

variable "release_version" {
  description = "Receiver release, e.g. \"anthropic-v0.1.0\". Selects the image tag."
  type        = string
}

variable "image" {
  description = "Full image reference. Overrides the one derived from release_version; use for locally built images during testing."
  type        = string
  default     = ""
}

variable "ghcr_username" {
  description = "GitHub username whose token can read the image package. The repository is private, so its packages are too: this is required unless the package has been made public."
  type        = string
  default     = ""
}

variable "ghcr_token" {
  description = "GitHub token (classic, scope read:packages) paired with ghcr_username. Sensitive; stored in Secret Manager."
  type        = string
  default     = ""
  sensitive   = true
}

variable "slashid_endpoint" {
  description = "SlashID base URL (e.g. https://api.slashid.com)."
  type        = string
}

variable "slashid_push_token" {
  description = "Push token of the SlashID Anthropic connection. Sensitive; stored in Secret Manager."
  type        = string
  sensitive   = true
}

variable "hook_signing_secret" {
  description = "whsec_… from claude.ai. Comma-join two values to accept both during a rotation. Sensitive."
  type        = string
  sensitive   = true
}

variable "policy_url" {
  description = "URL of the SlashID policy receiver (/ai-access/<id>). Empty skips the policy check."
  type        = string
  default     = ""
}

variable "preflight_enabled" {
  description = "Call SlashID preflight for the content check. Default false until the endpoint is live for your region; enabled against a 404 it takes the fail-mode path on every frame."
  type        = bool
  default     = false
}

variable "verdict_fail_mode" {
  description = "allow or deny when a check fails or answers unverified. Distinct from Anthropic's own failure handling."
  type        = string
  default     = "allow"

  validation {
    condition     = contains(["allow", "deny"], var.verdict_fail_mode)
    error_message = "verdict_fail_mode must be \"allow\" or \"deny\"."
  }
}

variable "enforce" {
  description = "Return deny verdicts. false is observe-only: checks run and are logged, allow is answered."
  type        = bool
  default     = false
}

variable "verdict_budget_ms" {
  type    = number
  default = 3500
}

variable "push_budget_ms" {
  type    = number
  default = 2000
}

variable "include_raw_content" {
  type    = bool
  default = false
}

variable "max_content_size" {
  type    = number
  default = 100000
}

variable "min_instances" {
  description = "Keep >= 1: a cold start inside Anthropic's verdict timeout is a webhook failure, and enough of those trip its circuit breaker."
  type        = number
  default     = 1
}

variable "max_instances" {
  type    = number
  default = 10
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "service_name" {
  type    = string
  default = "slashid-anthropic-forwarder"
}

variable "service_account_id" {
  type    = string
  default = "slashid-anthropic-sa"
}

variable "registry_repository_id" {
  description = "Artifact Registry remote repository that proxies ghcr.io."
  type        = string
  default     = "slashid-ghcr"
}
```

`main.tf`:

```hcl
provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  # ``anthropic-v0.1.0`` → ``0.1.0``: the release workflow tags the image with
  # the bare version from pyproject.toml.
  version_short = trimprefix(var.release_version, "anthropic-v")
  image = coalesce(
    var.image,
    "${var.region}-docker.pkg.dev/${var.project_id}/${var.registry_repository_id}/slashid/slashid-anthropic-forwarder:${local.version_short}"
  )
}

resource "google_project_service" "required" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  service                    = each.value
  disable_on_destroy         = false
  disable_dependent_services = false
}
```

`secrets.tf`:

```hcl
resource "google_secret_manager_secret" "push_token" {
  secret_id = "slashid_anthropic_push_token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "push_token" {
  secret      = google_secret_manager_secret.push_token.id
  secret_data = var.slashid_push_token
}

resource "google_secret_manager_secret" "signing_secret" {
  secret_id = "slashid_anthropic_signing_secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "signing_secret" {
  secret      = google_secret_manager_secret.signing_secret.id
  secret_data = var.hook_signing_secret
}

resource "google_secret_manager_secret" "ghcr_token" {
  count     = var.ghcr_username == "" ? 0 : 1
  secret_id = "slashid_anthropic_ghcr_token"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "ghcr_token" {
  count       = var.ghcr_username == "" ? 0 : 1
  secret      = google_secret_manager_secret.ghcr_token[0].id
  secret_data = var.ghcr_token
}
```

`registry.tf`:

```hcl
# Cloud Run pulls only from Artifact Registry, so a remote repository proxies
# ghcr.io where the release workflow publishes the image.
resource "google_artifact_registry_repository" "ghcr" {
  location      = var.region
  repository_id = var.registry_repository_id
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"

  remote_repository_config {
    description = "Proxy for ghcr.io"
    docker_repository {
      custom_repository {
        uri = "https://ghcr.io"
      }
    }
    dynamic "upstream_credentials" {
      for_each = var.ghcr_username == "" ? [] : [1]
      content {
        username_password_credentials {
          username                = var.ghcr_username
          password_secret_version = google_secret_manager_secret_version.ghcr_token[0].name
        }
      }
    }
    # The API validates the upstream credentials at create time, before the
    # service agent's read grant below has necessarily propagated.
    disable_upstream_validation = true
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.registry_reads_ghcr_token,
  ]
}

# The registry's service agent must be able to read the upstream token.
data "google_project" "this" {}

resource "google_secret_manager_secret_iam_member" "registry_reads_ghcr_token" {
  count     = var.ghcr_username == "" ? 0 : 1
  secret_id = google_secret_manager_secret.ghcr_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"

  # The service agent exists only once the API is enabled.
  depends_on = [google_project_service.required]
}
```

`iam.tf`:

```hcl
resource "google_service_account" "receiver" {
  account_id   = var.service_account_id
  display_name = "SlashID Anthropic forwarder"
}

resource "google_secret_manager_secret_iam_member" "push_token" {
  secret_id = google_secret_manager_secret.push_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "signing_secret" {
  secret_id = google_secret_manager_secret.signing_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

# Cloud Run pulls with its own service agent, which already holds
# roles/run.serviceAgent in-project; this grant matters only if the registry
# ever lives in another project. Kept so a cross-project move needs no IAM change.
resource "google_artifact_registry_repository_iam_member" "pull" {
  location   = google_artifact_registry_repository.ghcr.location
  repository = google_artifact_registry_repository.ghcr.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.receiver.email}"
}

# Anthropic calls the URL unauthenticated; the signature is the auth.
resource "google_cloud_run_v2_service_iam_member" "public" {
  location = google_cloud_run_v2_service.receiver.location
  name     = google_cloud_run_v2_service.receiver.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
```

`service.tf`:

```hcl
resource "google_cloud_run_v2_service" "receiver" {
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"
  # Provider 6 defaults this to true, which makes ``terraform destroy`` fail
  # until flipped. A stateless receiver has nothing to protect.
  deletion_protection = false

  template {
    service_account                  = google_service_account.receiver.email
    timeout                          = "30s"
    max_instance_request_concurrency = 20 # two outbound calls per request under budget, plus the push

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = local.image

      resources {
        limits   = { cpu = "1", memory = "512Mi" }
        cpu_idle = false # the event push runs after the response
      }

      env {
        name  = "SLASHID_ENDPOINT"
        value = var.slashid_endpoint
      }
      env {
        name  = "SLASHID_POLICY_URL"
        value = var.policy_url
      }
      env {
        name  = "SLASHID_PREFLIGHT_ENABLED"
        value = tostring(var.preflight_enabled)
      }
      env {
        name  = "SLASHID_VERDICT_FAIL_MODE"
        value = var.verdict_fail_mode
      }
      env {
        name  = "SLASHID_ENFORCE"
        value = tostring(var.enforce)
      }
      env {
        name  = "SLASHID_VERDICT_BUDGET_MS"
        value = tostring(var.verdict_budget_ms)
      }
      env {
        name  = "SLASHID_PUSH_BUDGET_MS"
        value = tostring(var.push_budget_ms)
      }
      env {
        name  = "SLASHID_INCLUDE_RAW_CONTENT"
        value = tostring(var.include_raw_content)
      }
      env {
        name  = "SLASHID_MAX_CONTENT_SIZE"
        value = tostring(var.max_content_size)
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
      env {
        name = "SLASHID_PUSH_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.push_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SLASHID_HOOK_SIGNING_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.signing_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.push_token,
    google_secret_manager_secret_iam_member.signing_secret,
    google_artifact_registry_repository_iam_member.pull,
    google_secret_manager_secret_version.push_token,
    google_secret_manager_secret_version.signing_secret,
  ]
}
```

`outputs.tf`:

```hcl
output "hook_url" {
  description = "Configure this as the Inference hooks endpoint in claude.ai (any path works; this one says what it is)."
  value       = "${google_cloud_run_v2_service.receiver.uri}/hooks/anthropic"
}

output "service_account_email" {
  value = google_service_account.receiver.email
}

output "image" {
  value = local.image
}
```

- [ ] **Step 2:** `cd anthropic/deploy/terraform && terraform fmt -recursive && terraform init -backend=false && terraform validate`. Expected: `Success!`. CI runs `terraform fmt -check -recursive`, so commit the formatted files.

- [ ] **Step 3:** README for the module (usage block mirroring `vertex/deploy/terraform/README.md`: module source with `?ref=anthropic-v0.1.0`, the required variables, that `ghcr_username`/`ghcr_token` are required while the package is private, that `preflight_enabled` defaults to false until the endpoint is live and `policy_url` stays unset until a gate route exists, the image tag convention `anthropic-v0.1.0` → `:0.1.0`, and the claude.ai setup order: apply, copy `hook_url`, configure endpoint, store secret, re-apply with `hook_signing_secret`, Test connection, Shadow mode, enforce).

- [ ] **Step 4: Commit** — `git add anthropic/deploy/terraform && git commit -m "feat(anthropic): Terraform module for Cloud Run"`.

### Task 6.2: Release workflow and CI

**Files:**
- Create: `.github/workflows/release-anthropic.yml`
- Modify: `.github/workflows/ci.yml` (matrix + terraform job)

- [ ] **Step 1:** `release-anthropic.yml`:

```yaml
name: Release Anthropic

on:
  push:
    tags: ["anthropic-v*"]

permissions:
  contents: write
  packages: write

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Resolve and verify version
        run: |
          TAG_VERSION="${GITHUB_REF_NAME#anthropic-v}"
          FILE_VERSION="$(python3 -c 'import tomllib, pathlib; print(tomllib.loads(pathlib.Path("anthropic/pyproject.toml").read_text())["project"]["version"])')"
          if [ "$TAG_VERSION" != "$FILE_VERSION" ]; then
            echo "::error::Tag $GITHUB_REF_NAME expects version $TAG_VERSION but anthropic/pyproject.toml has $FILE_VERSION"
            exit 1
          fi
          echo "VERSION=$FILE_VERSION" >> "$GITHUB_ENV"

      - uses: docker/setup-buildx-action@v3

      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Determine pre-release
        id: prerelease
        run: |
          if [[ "${VERSION}" == *-* ]]; then echo "value=true" >> "$GITHUB_OUTPUT"; else echo "value=false" >> "$GITHUB_OUTPUT"; fi

      - uses: docker/build-push-action@v6
        with:
          context: .
          file: anthropic/Dockerfile
          push: true
          # Terraform pins by version; no floating tag, so a pre-release
          # cannot move anything a customer resolves.
          tags: ghcr.io/slashid/slashid-anthropic-forwarder:${{ env.VERSION }}

      - uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          prerelease: ${{ steps.prerelease.outputs.value }}
          body: |
            Image: `ghcr.io/slashid/slashid-anthropic-forwarder:${{ env.VERSION }}`
```

- [ ] **Step 2:** In `ci.yml`: change `subproject: [shared, bedrock, vertex]` to `subproject: [shared, bedrock, vertex, anthropic]`; change the "Sync workspace" step to

```yaml
      - name: Sync workspace
        # --locked: a plain sync silently rewrites a stale uv.lock, and the
        # anthropic Dockerfile runs ``uv sync --frozen``, so a stale lock
        # would fail the release build instead of this job.
        run: uv sync --all-groups --locked
```

and turn the `terraform` job into a matrix over both modules:

```yaml
  terraform:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        dir: [vertex/deploy/terraform, anthropic/deploy/terraform]
    defaults:
      run:
        working-directory: ${{ matrix.dir }}
    steps:
      - uses: actions/checkout@v4
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.15.8"
      - run: terraform fmt -check -recursive
      - run: terraform init -backend=false
      - run: terraform validate
```

- [ ] **Step 3: Commit** — `git add .github && git commit -m "ci(anthropic): release image to GHCR; run the subproject in CI"`.

### Task 6.3: README

**Files:**
- Create: `anthropic/README.md`
- Modify: `README.md` (root: add the component and its release tag convention)

- [ ] **Step 1:** Write `anthropic/README.md` in the Vertex README's shape: what it is, the verdict flow, **Prerequisites** (Claude Enterprise; Owner or Primary owner to configure hooks; a public `https://` endpoint, no tunnels; SlashID-side: `SLASHID_POLICY_URL` unset until a gate route exists, preflight off until PR #7733 is deployed), **Known limitations** (the tail gap: the last response of a session and the tools it consumed are never reported, and a single-turn session emits nothing — say plainly that the verdict still ran, so this is a gap in the audit record rather than in inspection; under a rollout percentage below 100, an invocation whose successor turn was unsampled is never emitted; no tool or MCP-server metadata reaches the receiver: the frame carries no tool definitions, so `available_tools` and `available_tool_servers` are synthesized from the names of tools actually **used**, carry no description or schema, omit every declared-but-unused tool, and surface an MCP server only when a client names its tools `mcp__server__tool` — say that the Vertex and Bedrock forwarders read real declarations from the request body, so absence here means unobservable rather than unused; attachment digests are of what Claude stored, not always what the user uploaded — a measured image came back 2 KB larger as a processed copy, and some documents are stored as extracted text — so such a hash will not match the original file and no flag marks which is which; hash matching from a frame alone covers plain text only; server-tool results arrive as `[non-text content]` placeholders and claude.ai's extended research task emits no frames at all, so neither is inspectable; claude.ai attachments arrive nameless except through the `<uploaded_files>` block, and the fallback name is that path's basename; `media_type` on a `document` block is `None` for values outside the IANA registry such as `text/x-python`, while the `accessed_files` entry keeps the raw string; Cloud Run's 32 MiB HTTP/1 cap; Compliance API enrichment is a later pull pass), the two failure-mode knobs and which covers what (`SLASHID_VERDICT_FAIL_MODE` vs Anthropic's failure handling), `SLASHID_ENFORCE` vs Anthropic's shadow mode, the budget paragraph (Anthropic retries once, only on connection failure; the push budget makes the sink's `SLASHID_REQUEST_TIMEOUT_SECONDS` / `SLASHID_MAX_RETRIES` inert), development commands, the configuration table (every `SLASHID_*` variable in `config.py` including the inherited ones, plus `LOG_LEVEL`), marking which are container-env-only because Task 6.1's module exposes no Terraform variable for them (`HOOK_ALLOW_UNSIGNED`, `MAX_BODY_BYTES`, `CAPTURE_BUCKET`, `CAPTURE_DENY_MARKER`, `REQUEST_TIMEOUT_SECONDS`, `MAX_RETRIES`), and the claude.ai setup order.
- [ ] **Step 2:** Root README: add the `anthropic/` bullet and the `anthropic-vX.Y.Z` release line.
- [ ] **Step 3: Commit** — `git add README.md anthropic/README.md && git commit -m "docs(anthropic): README and known limitations"`.

### Task 6.4: Full gate and PR

- [ ] **Step 1:** From the root, what CI runs, for every subproject: `uv lock --check && for d in shared bedrock vertex anthropic; do (cd $d && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q) || exit 1; done`, then the Terraform validate from 6.1 for both modules. The shared schema change must not move Bedrock or Vertex. `anthropic` ends at **97** tests: config 6, signature 10, main 16, frame 9, event_envelope 21, policy 7, preflight 10, verdict 18.
- [ ] **Step 2:** Build and smoke the container locally:

```bash
docker build -f anthropic/Dockerfile -t slashid-anthropic-forwarder:dev .
SECRET=$(cd anthropic && uv run python -c "from tests.conftest import SECRET; print(SECRET)")
cid=$(docker run -d --rm -p 18080:8080 -e SLASHID_ENDPOINT=https://api.slashid.com -e SLASHID_PUSH_TOKEN=x \
  -e "SLASHID_HOOK_SIGNING_SECRET=$SECRET" -e SLASHID_PREFLIGHT_ENABLED=false slashid-anthropic-forwarder:dev)
for i in $(seq 1 20); do curl -s -o /dev/null http://127.0.0.1:18080/ && break; sleep 0.5; done
(cd anthropic && uv run python - <<'EOF'
import json, pathlib, urllib.request
from tests.conftest import sign  # the fixture function; call its inner signer
body = pathlib.Path("tests/fixtures/frame_tool_result.json").read_bytes()
headers = sign.__wrapped__()(body, "msg_011CfFXrZo19wubUcJjnSJa9")
req = urllib.request.Request("http://127.0.0.1:18080/hooks/anthropic", data=body, method="POST", headers=headers)
print(urllib.request.urlopen(req).read())
EOF
)
docker logs "$cid" | tail -5; docker stop "$cid"
```

Expected: `b'{"action":"allow"}'` and a `push failed` line in the logs (the placeholder token is rejected; that is the isolation working).
- [ ] **Step 3:** Open the PR against `main` linking the spec and listing the verification items still open (1, 3, 4, 5, 7, 8; 6 for the enforced case). Do not merge; wait for review.
