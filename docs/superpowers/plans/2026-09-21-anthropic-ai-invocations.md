# Anthropic AI Invocations Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Cloud Run service that observes Claude Enterprise AI invocations through two independent capabilities — an inline inference hook that also answers allow/deny, and a scheduled reader of the Compliance API — joining both into `AIInvocationObservedV1` events pushed to SlashID.

**Architecture:** A uv workspace member `anthropic/` beside `bedrock/` and `vertex/`. Neither capability pushes from the request path. Both write into a Firestore-backed **pending store**, and a claim decides who pushes. The two sources can only be joined on a model-minted `tool_use.id`, which was measured, so they are divided by **ownership** rather than left to meet in the middle: the hook owns every invocation it sees, and the reader only touches runs that carry a tool call. Capabilities switch on by which credentials are present, so hook-only, compliance-only and both are supported configurations of the same image.

**Tech Stack:** Python 3.13, uv workspace, pydantic 2.13, FastAPI + uvicorn, httpx, google-cloud-firestore, pytest + pytest-asyncio (auto mode), ruff (line-length 100), ty, Docker (wolfi), Cloud Run v2 and Cloud Scheduler via Terraform.

**Spec:** `docs/superpowers/specs/2026-09-21-anthropic-ai-invocations-design-v2.md`. Read it first. Its "What the wire actually looks like" section is what the fixtures encode, and every number in it was measured against a live tenant across a 492-frame corpus.

**Supersedes:** `2026-09-20-anthropic-inference-hooks-receiver.md`, written against a stateless receiver that pushed on every frame. Its Chunk 1 shipped and is recorded below as done.

---

## Before you start

Decisions already made. Reversing one silently breaks something:

1. **Nothing is stateless.** The receiver never pushes from the request path. It writes a pending record and returns. This was a design goal that measurement retired: `accessed_files` belong to the round the model consumed, so a session's final `Read` has no successor frame, and without a deadline flush that read is never recorded at all.
2. **Only a `tool_use.id` joins the two sources.** Measured on a session present in both: transcript-prefix digests gave 200 keys on the frame side and 302 on the reader side, with **zero in common**, unchanged after dropping synthetic markers and after removing all text. The stored transcript is a different projection of the conversation. A run with a tool call is joinable (194 of 284); one without is owned by the hook alone and the reader must never emit it.
3. **The record is stored as a serialized mapping, not a validated model.** `AIInvocationObservedV1` requires `parsed_as`, which a pending record cannot know, and its base sets `extra="forbid"`. Validation happens at push, on the one path that can log and retry.
4. **Push first, then tombstone, but a duplicate is not free.** Measured against `ng-evangelion`: dedup is keyed on `{org}:{connection}:{request_id}` and is **first-completed-wins**, 72 hours, sliding, Redis-backed. It guards the graph and BigQuery writes only — each delivery still stores a raw event and re-runs the detections engine, so duplicates can mean duplicate alerts. Two different keys for one invocation are counted twice, never reconciled, and the usage accumulators are not idempotent. So a push is a commitment that cannot be topped up later, and the ordering still favours the duplicate only because a missing audit record is worse.
5. **An eventing failure never becomes a verdict failure.** The push runs after the response; a non-200 from us is a webhook failure that hands control to the customer's own fail-open/fail-closed setting.
6. **Attribution runs one round behind, and this is the easiest thing here to get wrong.** The record names the *previous* assistant run, so `input` ends before that run and `used_tools`/`accessed_files` come from the round **it** consumed. `after_last_assistant()` returns the *fresh* round, which drives the verdict and belongs to the tail record, and must contribute nothing to the emitted event. An attachment in the fresh round landing on the previous run was a real bug caught in review, on nearly every claude.ai frame. Hand the builder a transcript truncated before the trailing run and the shared helpers land correctly by themselves.
7. **Every frame writes a tail record and its successor discards it.** The final round of a session has no successor to report it. The tail is keyed on a hook-local digest of the whole transcript, which is exact within one source — 239 distinct keys from 284 deliveries, zero false merges or splits — and the successor reconstructs that key by dropping its own trailing run and the round after it. No per-session pointer: that would collide across the sub-conversations sharing a `session_id`.
8. **The signature crypto is not ours.** `signature.py` wraps the `standardwebhooks` reference library, which owns the HMAC, the `whsec_` prefix, the base64 alphabet and the ±300 s tolerance. Do not reimplement it.
9. **Unknown top-level `type` and `config-test` frames bypass both checks and answer allow.** Unknown content blocks are skipped, never rejected.

Toolchain, from each subproject directory (what CI runs):

```bash
cd anthropic && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
cd shared    && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
cd vertex    && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest
```

`vertex/` is in that list because Chunk 5 promotes `CheckpointStore` out of it into `shared/`.

Fixtures under `anthropic/tests/fixtures/` are sanitized captures from a live tenant. Their shapes are authoritative: when a test disagrees with a fixture, the test is wrong. Note two traps they encode — `frame_attachment.json` carries three `attachment` blocks in one user message with `file_name` and `size_bytes` null on two of them, and `frame_mcp_tool.json` carries two consecutive user messages.

## File structure

```
shared/src/slashid_ai_forwarder_core/
├── events.py                          # + AnthropicIdentityDetails in the union
│                                      # + AIAccessedFile.provenance
│                                      # + EventEnvelope.conversation_id
├── checkpoint.py                      # NEW: Checkpoint + CheckpointStore, promoted from vertex/
├── normalize/anthropic/
│   ├── schema.py                      # + AliasChoices on tool_use.name; + AnthropicAttachmentBlock
│   └── normalize.py                   # + attachment → document
└── normalize/normalized/
    ├── media_types.py                 # parse_media_type actually falls back to None
    └── tool_results.py                # stamps provenance="tool_result"

anthropic/
├── pyproject.toml                     # + google-cloud-firestore
├── Dockerfile                         # (exists)
├── README.md                          # NEW
├── deploy/
│   ├── cloudbuild.yaml, dev-deploy.sh # (exist) dev iteration path
│   └── terraform/                     # NEW: customer path
└── src/slashid_anthropic_forwarder/
    ├── config.py                      # (exists) + store, compliance and enrichment knobs
    ├── main.py                        # (exists, rewritten) POST /{path} hook, POST /tick
    ├── address.py                      # toolu anchor, hook: fallback, tail: digest
    ├── record.py                      # the partial event + control envelope
    ├── store.py                       # PendingStore protocol + FirestorePendingStore
    ├── pending.py                      # readiness, the flush, tail supersession
    ├── hook/
    │   ├── signature.py               # (exists at package root; MOVED here)
    │   ├── capture.py                 # (exists at package root; MOVED here)
    │   ├── frame.py                   # PromptFrame + split_transcript
    │   ├── checks.py policy.py preflight.py verdict.py
    │   └── envelope.py                # frame → partial record
    └── compliance/
        ├── client.py                  # three feeds, three query vocabularies
        ├── denials.py                 # Reader A
        ├── responses.py               # Reader B
        └── attachments.py             # md5 from the listing, or download and digest
```

Two existing modules move. `signature.py` and `capture.py` sit at the package root today; `anthropic/tests/test_signature.py:10` imports the first by that path and `main.py:18` imports the second. Both imports change with the move, in the chunk that makes it.

`checks.py` is one file the spec's layout does not name. `policy.py` and `preflight.py` both return a verdict and raise the same failure, and `verdict.py` imports both, so the shared types cannot live in any of the three without a cycle.

---

## Chunk 1: Scaffold, config, signature, capture receiver — DONE

Landed on `paulo/anthropic-receiver` (`4b2e303`, `c36b2ab`, `5cd5b66`, `e9cbf96`, `cc4b156`, `95a5062`, `5c5ca22`, `2bbb879`, `f3dfeea`, `ec196ac`). Recorded so the plan is complete; nothing to do. A clean checkout of `main` does **not** have these: start from the branch.

- [x] `anthropic/pyproject.toml`, root workspace member, `Config(BaseConfig)` with `signing_secrets`, fail mode, budgets, `shadow_mode`, `max_body_bytes` and capture knobs (`tests/test_config.py`).
- [x] `signature.py` Standard Webhooks verification delegating the crypto to the `standardwebhooks` library, accepting N secrets for rotation (`tests/test_signature.py`, 10 tests).
- [x] `capture.py` + `main.py` capture receiver: 401 unsigned, 413 oversized, allow on unknown type, deny marker, capture failure isolated (`tests/test_main.py`, 10 tests).
- [x] `Dockerfile`, `deploy/cloudbuild.yaml`, `deploy/dev-deploy.sh`; deployed to `strong-hue-507702-k7`, driven with `claude-work`, and 492 frames captured. Nine sanitized fixtures written from that corpus.
- [x] `SLASHID_ENFORCE` renamed to `SLASHID_SHADOW_MODE` with inverted sense and a safe default (`ec196ac`).

---

## Chunk 2: Shared library additions

Five additions to `shared/` plus one bug fix, all of them prerequisites for the Anthropic receiver and its compliance reader. Two are silent-data-loss fixes rather than features: a hook `tool_use` block fails `AnthropicToolUseBlock` validation today (it spells the tool name `tool_name`), smart-unions down to `AnthropicUnknownBlock`, and is then dropped on the floor by `_translate_request_content` — so *every* hook tool call is currently invisible; the same fate befalls `attachment` blocks. The other three are new wire surface: `AnthropicIdentityDetails` in the `identity_details` union, `AIAccessedFile.provenance` so the reader can replace the `attachment` group without touching `tool_result`, and `conversation_id` on `EventEnvelope`, which the envelope has never carried even though `AIInvocationObservedV1` has had the field all along. The bug fix is `parse_media_type`, whose `except ValueError` is dead code: the failure it means to swallow surfaces two frames later at the `NormalizedContent` boundary instead. Order matters — Task 2.2 must land before Task 2.3, because an attachment's `media_type` goes straight into a `NormalizedContent`.

### Task 2.1: `tool_use.name` accepts the hook's `tool_name`

The Messages API spells the tool name `name`; the Inference hooks frame spells it `tool_name` (confirmed in `anthropic/tests/fixtures/frame_tool_result.json`, which carries `{"type": "tool_use", "id": "toolu_01Dqhr…", "tool_name": "Read", "input": {…}}`). This is not a cosmetic rename. `AnthropicToolUseBlock.name` is required, so the block fails validation, the bare smart-union at `schema.py:229-235` falls through to `AnthropicUnknownBlock` (`schema.py:48-55`, `type: str` matches anything), and `_translate_request_content` has no case for it — `normalize.py:178` is a bare comment, "AnthropicUnknownBlock: skipped silently". Net effect today: a hook frame's assistant turn normalizes to an empty content list. Verified by hand against the real models. The test therefore asserts the block *survives translation*, not merely that it parses.

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/schema.py:7` (the `from pydantic import` line) and `:35-39` (`AnthropicToolUseBlock`)
- Test: `shared/tests/normalize/test_schemas_anthropic.py`, `shared/tests/normalize/test_anthropic_normalize.py`

- [ ] **Step 1: Write the failing schema test** — append to `shared/tests/normalize/test_schemas_anthropic.py`:

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
    """Without the alias the block misses ``AnthropicToolUseBlock`` and the
    bare smart-union hands it to ``AnthropicUnknownBlock`` instead."""
    from slashid_ai_forwarder_core.normalize.anthropic.schema import AnthropicRequestMessage

    msg = AnthropicRequestMessage.model_validate(
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "tool_name": "Read"}]}
    )
    assert isinstance(msg.content[0], AnthropicToolUseBlock)
```

- [ ] **Step 2: Write the failing translation test** — this is the one that states the actual bug. Append to `shared/tests/normalize/test_anthropic_normalize.py`:

```python
async def test_hook_spelled_tool_use_survives_request_translation() -> None:
    """Regression: a hook-spelled tool_use used to validate as
    AnthropicUnknownBlock and get dropped silently by
    ``_translate_request_content`` — every hook tool call went missing."""
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
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01Dqhr",
                            "tool_name": "Read",
                            "input": {"file_path": "/home/alice/proj/notes.txt"},
                        }
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
    assert [b.kind for b in blocks] == ["tool_use"]
    assert blocks[0].tool_name == "Read"
    assert blocks[0].tool_use_id == "toolu_01Dqhr"
    assert blocks[0].tool_input == {"file_path": "/home/alice/proj/notes.txt"}
```

- [ ] **Step 3: Run to verify both fail**

Run: `cd shared && uv run pytest tests/normalize/test_schemas_anthropic.py tests/normalize/test_anthropic_normalize.py -k "hook_spelling or hook_spelled" -v`

Expected: 3 failures.
- `test_tool_use_block_accepts_hook_spelling_of_name` — `ValidationError: 1 validation error for AnthropicToolUseBlock / name / Field required [type=missing, …]`
- `test_request_union_picks_tool_use_for_hook_spelling` — `AssertionError` on `isinstance`; the object is an `AnthropicUnknownBlock(type='tool_use')`
- `test_hook_spelled_tool_use_survives_request_translation` — `AssertionError: assert [] == ['tool_use']`

- [ ] **Step 4: Implement** — in `schema.py:7` change the import to `from pydantic import AliasChoices, Field, JsonValue`, and rewrite `AnthropicToolUseBlock` (`:35-39`):

```python
class AnthropicToolUseBlock(_LenientModel):
    type: Literal["tool_use"]
    id: str
    # The Messages API spells it ``name``; the Inference hooks frame
    # ``tool_name``. Without both spellings the block misses this class,
    # smart-unions to AnthropicUnknownBlock and is dropped by
    # ``_translate_request_content``.
    name: str = Field(validation_alias=AliasChoices("name", "tool_name"))
    input: JsonValue = None  # absent on stream start, filled by input_json_delta
```

- [ ] **Step 5: Run to verify it passes, and nothing else broke**

Run: `cd shared && uv run pytest -q && uv run ty check`

Expected: all pass. `AliasChoices` keeps `name=` working, so the existing schema, normalize and stream suites — which all construct the block with `name=` — are unaffected.

- [ ] **Step 6: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/anthropic/schema.py \
        shared/tests/normalize/test_schemas_anthropic.py \
        shared/tests/normalize/test_anthropic_normalize.py
git commit -m "fix(shared): tool_use.name accepts the hook's tool_name spelling"
```

### Task 2.2: `parse_media_type` actually rejects unregistered types

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/normalized/media_types.py:1-40` (module docstring, imports, `parse_media_type`)
- Test: `shared/tests/normalize/normalized/test_parse_media_type.yaml`

Precise diagnosis, verified against the pinned `pydantic-extra-types`: `MimeType` (`pydantic_extra_types/mime_types.py:2496`) is a plain `str` subclass. Its registry lookup lives in `_validate`, a classmethod wired in through `__get_pydantic_core_schema__` and therefore called **only by pydantic**. Direct construction never raises — `MimeType("txt")` returns `'txt'` — so the `except ValueError` at `media_types.py:38-40` is dead code and the function's promise to return `None` is never kept. The error instead surfaces two frames later at the model boundary: `NormalizedContent.media_type: MimeType | None` (`normalize/normalized/types.py:43`) raises `ValidationError: Invalid MIME type [type=mime_type, …]`. The module docstring (`:6-8`) and the function docstring (`:28-29`) both say registry validity is not enforced; both are false — it *is* enforced, just at the wrong place and by exploding. Real traffic hits this: a live compliance listing carried `"mime_type": "txt"`. The existing case table has no unregistered case at all.

`parse_media_type` has no production call sites yet (grep: definition and tests only), so this is a free fix — and a required one, because Task 2.3 feeds attachment media types straight into `NormalizedContent`.

- [ ] **Step 1: Write the failing cases** — append to `shared/tests/normalize/normalized/test_parse_media_type.yaml`:

```yaml
---
id: unregistered_shorthand
# Real value seen in a compliance file listing: a bare extension, not a
# media type. Must degrade to None, not raise at the model boundary.
raw: txt
expected: null
---
id: unregistered_well_formed
raw: not/a-real-type
expected: null
---
id: registered_uppercase_canonicalizes
# MimeType._validate looks up case-insensitively and returns the
# registry's own casing.
raw: TEXT/PLAIN
expected: text/plain
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run pytest tests/normalize/normalized/test_parse_media_type.py -v`

Expected: 3 new failures, the 5 pre-existing cases pass.
- `[unregistered_shorthand]` — `AssertionError: assert 'txt' == None`
- `[unregistered_well_formed]` — `AssertionError: assert 'not/a-real-type' == None`
- `[registered_uppercase_canonicalizes]` — `AssertionError: assert 'TEXT/PLAIN' == 'text/plain'`

- [ ] **Step 3: Implement** — rewrite `media_types.py`, replacing the bare constructor with a module-level `TypeAdapter` and correcting both docstrings:

```python
"""Media-type parsing with tolerant preprocessing.

Wraps ``pydantic_extra_types.mime_types.MimeType`` with tolerant
preprocessing: strips parameters (``text/plain; charset=utf-8`` →
``text/plain``) and whitespace, returns ``None`` on empty or on a value
the IANA registry doesn't know. Validation runs through a TypeAdapter
because ``MimeType`` is a plain ``str`` subclass whose registry check
lives in a pydantic validator — constructing one directly never raises,
it just defers the failure to the first model that holds it.
"""

from __future__ import annotations

import logging

from pydantic import TypeAdapter, ValidationError
from pydantic_extra_types.mime_types import MimeType

log = logging.getLogger(__name__)

_MIME = TypeAdapter(MimeType)


def parse_media_type(raw: str | None) -> MimeType | None:
    """Parse ``raw`` into a ``MimeType``; return ``None`` on empty input.

    Handles the two common wire quirks:
      - parameters (``"text/plain; charset=utf-8"`` — RFC 6838 §4.3) —
        stripped before validation
      - surrounding whitespace — stripped

    Unregistered values (a bare ``"txt"`` from a compliance file listing,
    a vendor-invented type) return ``None``. Returning the raw string
    would only move the failure to ``NormalizedContent.media_type``,
    which validates through the same registry and raises there.
    """
    if not raw:
        return None
    base = raw.split(";", 1)[0].strip()
    if not base:
        return None
    try:
        return _MIME.validate_python(base)
    except ValidationError:
        log.debug("unrecognized media type: %r", raw)
        return None
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd shared && uv run pytest tests/normalize/normalized/test_parse_media_type.py -v && uv run ty check`

Expected: 8 passed. The registered cases (`text/plain`, parameterized, whitespace-padded `application/json`) still round-trip; the three new ones now behave.

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/normalized/media_types.py \
        shared/tests/normalize/normalized/test_parse_media_type.yaml
git commit -m "fix(shared): parse_media_type rejects unregistered types instead of deferring"
```

### Task 2.3: `AnthropicAttachmentBlock` and its translation to `document`

Same silent-drop story as Task 2.1: an `attachment` block has no modelled variant, lands on `AnthropicUnknownBlock`, and disappears in `_translate_request_content`. Wire shape measured from `anthropic/tests/fixtures/frame_attachment.json`:

```json
{"type": "attachment", "media_type": "text/plain", "size_bytes": null,
 "file_name": "maria.txt", "text": "Maria tinha um carneirinho\n"}
```

Two things that fixture settles and the model must tolerate: `size_bytes` and `file_name` are null on some blocks (null `file_name` on both the JPEG and the PDF; null `size_bytes` on the text and the PDF), and one user message carried **three** attachment blocks sandwiched between two text blocks, so ordering and interleaving matter. Hook attachments carry extracted text, never bytes — `byte_length` is the length of that text, the same bytes the receiver hashes, not the frame's `size_bytes`.

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/schema.py` (new class after `AnthropicToolResultBlock` at `:215-227`; the `AnthropicRequestContentBlock` union at `:229-235`)
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/normalize.py:27-46` (imports) and `:148-179` (`_translate_request_content`)
- Test: `shared/tests/normalize/test_anthropic_normalize.py`

- [ ] **Step 1: Write the failing tests** — append to `shared/tests/normalize/test_anthropic_normalize.py`:

```python
async def test_attachment_blocks_become_documents_in_frame_order() -> None:
    """Shape and ordering from anthropic/tests/fixtures/frame_attachment.json:
    three attachments between two text blocks. ``byte_length`` is the length
    of the extracted text — the bytes the receiver hashes — not the frame's
    ``size_bytes``, which is null for text uploads anyway."""
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
                        {"type": "text", "text": "<uploaded_files>\n</uploaded_files>\n\n"},
                        {
                            "type": "attachment",
                            "media_type": "text/plain",
                            "size_bytes": None,
                            "file_name": "maria.txt",
                            "text": "Maria tinha um carneirinho\n",
                        },
                        {
                            "type": "attachment",
                            "media_type": "image/jpeg",
                            "size_bytes": 70657,
                            "file_name": None,
                            "text": None,
                        },
                        {
                            "type": "attachment",
                            "media_type": "application/pdf",
                            "size_bytes": None,
                            "file_name": None,
                            "text": "Extracted document text\r\nline two\r\nline three\r\n",
                        },
                        {"type": "text", "text": "teste com anexos"},
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
    assert [b.kind for b in blocks] == ["text", "document", "document", "document", "text"]
    assert blocks[1].text == "Maria tinha um carneirinho\n"
    assert blocks[1].byte_length == 27  # not size_bytes, which is null here
    assert blocks[1].media_type == "text/plain"
    # An image arrives with no name and no text: metadata only, nothing to hash.
    assert blocks[2].text is None
    assert blocks[2].byte_length is None
    assert blocks[2].media_type == "image/jpeg"
    # A PDF arrives as extracted text with CRLF line endings, preserved verbatim.
    assert blocks[3].byte_length == 47


async def test_attachment_with_unregistered_media_type_is_not_fatal() -> None:
    """A compliance listing carried ``"mime_type": "txt"``. The block must
    still translate, with media_type dropped rather than raising."""
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
                    "content": [{"type": "attachment", "media_type": "txt", "text": "x"}],
                }
            ]
        }
    )
    response = AnthropicMessage.model_validate(
        {"type": "message", "role": "assistant", "content": [], "stop_reason": "end_turn"}
    )
    normalized = await message_to_normalized_invocation(request, response, config=_config())
    block = normalized.input.messages[0].content[0]
    assert block.kind == "document"
    assert block.media_type is None
    assert block.byte_length == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run pytest tests/normalize/test_anthropic_normalize.py -k attachment -v`

Expected: 2 failures.
- `test_attachment_blocks_become_documents_in_frame_order` — `AssertionError: assert ['text', 'text'] == ['text', 'document', 'document', 'document', 'text']`; the three attachments validated as `AnthropicUnknownBlock(type='attachment')` and were skipped
- `test_attachment_with_unregistered_media_type_is_not_fatal` — `IndexError: list index out of range` (the message translated to an empty content list)

- [ ] **Step 3: Implement the schema** — in `schema.py`, after `AnthropicToolResultBlock`:

```python
class AnthropicAttachmentBlock(_LenientModel):
    """Inference hooks attachment: metadata plus extracted text, never bytes.

    Every field but ``type`` can be null — an image arrives with no name
    and no text, a PDF with text but no name. ``size_bytes`` describes
    the upload, not the extracted text, and disagrees with it whenever
    the stored copy was processed; it is metadata, not a hash input.
    """

    type: Literal["attachment"]
    file_name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    text: str | None = None
```

and widen the request-side union at `:229-235`, keeping `AnthropicUnknownBlock` last:

```python
AnthropicRequestContentBlock = (
    AnthropicTextBlock
    | AnthropicToolUseBlock
    | AnthropicThinkingBlock
    | AnthropicToolResultBlock
    | AnthropicAttachmentBlock
    | AnthropicUnknownBlock
)
# Superset of the response-side content-block union: adds
# AnthropicToolResultBlock (user-turn only) and AnthropicAttachmentBlock
# (Inference hooks only).
```

- [ ] **Step 4: Implement the translation** — in `normalize.py`, add `AnthropicAttachmentBlock` to the `.schema` import list (alphabetically first, before `AnthropicContentBlockDeltaEvent`) and add `from ..normalized.media_types import parse_media_type` next to the `..normalized.tools` import. Then add a case to `_translate_request_content`, before the `# AnthropicUnknownBlock: skipped silently.` comment:

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

- [ ] **Step 5: Run to verify it passes**

Run: `cd shared && uv run ruff format . && uv run ruff check --fix . && uv run pytest -q && uv run ty check`

Expected: all pass. Note the `case` must sit after `AnthropicToolResultBlock` and before the unknown-block comment; match order follows union order and `AnthropicUnknownBlock` has no case at all.

- [ ] **Step 6: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/anthropic/ \
        shared/tests/normalize/test_anthropic_normalize.py
git commit -m "feat(shared): hook attachment block normalizes to a document"
```

### Task 2.4: `AnthropicIdentityDetails` in the identity union

`_WireModel` is `extra="forbid"` (`events.py:61`), so an Anthropic identity cannot be smuggled through an existing variant — it has to be a modelled sibling of `AWSIdentityDetails` (`events.py:163-187`) and `GCPIdentityDetails` (`events.py:206-221`). The design's field mapping fixes the shape: `{kind: "anthropic", user_id: actor.id}`, and a null `actor.id` drops the event rather than emitting an identity the server's resolver would reject. `user_id` is therefore required, not optional — the drop decision belongs in the receiver's envelope constructor, the same place `bedrock_envelope` and `vertex_envelope` make theirs.

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/events.py` (new class after `GCPIdentityDetails` at `:206-221`; the `IdentityDetails` alias at `:224-227`)
- Test: `shared/tests/test_events.py`

- [ ] **Step 1: Write the failing test** — append to `shared/tests/test_events.py`:

```python
def test_anthropic_identity_details_round_trips_through_the_union() -> None:
    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    event = AIInvocationObservedV1.model_validate(
        {
            "request_id": "r",
            "timestamp": "2026-09-18T00:00:00Z",
            "identity_details": {"kind": "anthropic", "user_id": "user_01AbCdEfGhIjKlMnOpQrStUv"},
            "model": {"id": "claude-sonnet-5"},
            "parsed_as": "anthropic-inference-hook",
        }
    )
    assert isinstance(event.identity_details, AnthropicIdentityDetails)
    assert event.identity_details.user_id == "user_01AbCdEfGhIjKlMnOpQrStUv"
    assert event.model_dump(exclude_none=True)["identity_details"] == {
        "kind": "anthropic",
        "user_id": "user_01AbCdEfGhIjKlMnOpQrStUv",
    }


def test_anthropic_identity_details_requires_a_user_id() -> None:
    """A null ``actor.id`` must drop the event upstream — the server rejects
    an identity with no identifier, so the model refuses to build one."""
    from pydantic import ValidationError

    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    with pytest.raises(ValidationError):
        AnthropicIdentityDetails.model_validate({"kind": "anthropic"})


def test_anthropic_identity_details_on_the_envelope() -> None:
    """EventEnvelope shares the union, so the receiver's envelope
    constructor populates it the same way bedrock/vertex do theirs."""
    from slashid_ai_forwarder_core.events import AnthropicIdentityDetails

    env = EventEnvelope(
        request_id="r",
        timestamp="2026-09-18T00:00:00Z",
        identity_details=AnthropicIdentityDetails(user_id="user_01Abc"),
        model=AIModel(id="claude-sonnet-5"),
        parsed_as="anthropic-inference-hook",
    )
    assert env.identity_details.kind == "anthropic"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run pytest tests/test_events.py -k anthropic_identity -v`

Expected: 3 failures — `ImportError: cannot import name 'AnthropicIdentityDetails'`. (Had the import existed, the first would fail on the discriminator instead: `Input tag 'anthropic' found using 'kind' does not match any of the expected tags: 'aws', 'gcp'`.)

- [ ] **Step 3: Implement** — in `events.py`, after `GCPIdentityDetails`:

```python
class AnthropicIdentityDetails(_WireModel):
    """Anthropic-source shape of ``AIInvocationObservedV1.identity_details``.

    The Inference hooks frame names the acting principal as ``actor.id``,
    a ``user_01…`` identifier stable across requests; the compliance
    denial activity names the same principal as ``actor.user_id``.
    Required, not optional: the server's resolver rejects a payload with
    no identifier, so a frame whose actor id is null is dropped by the
    envelope constructor rather than turned into an unusable event.
    ``kind`` is a client-side discriminator; the server ignores it.
    """

    kind: Literal["anthropic"] = "anthropic"
    user_id: str
```

and widen the alias at `:224-227`:

```python
IdentityDetails = Annotated[
    AWSIdentityDetails | GCPIdentityDetails | AnthropicIdentityDetails,
    Field(discriminator="kind"),
]
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd shared && uv run pytest -q && uv run ty check`

Expected: all pass, including the existing AWS/GCP discriminator tests at `tests/test_events.py:827-962` — adding a third tag doesn't change how the first two dispatch.

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/events.py shared/tests/test_events.py
git commit -m "feat(shared): AnthropicIdentityDetails in the identity_details union"
```

### Task 2.5: `AIAccessedFile.provenance`

The reader's replace rule needs to tell a user upload from a tool read: for a message with a `files[]` listing, entries whose provenance is `attachment` are rebuilt from the listing (better on every field), and `tool_result` entries are never touched. A flat list makes the two indistinguishable. The literal is `tool_result | attachment`; `generated` is **reserved and not emitted** — `generated_files` enrichment is deferred per the spec's open question, so shipping the value would advertise something no producer sets.

The only producer to stamp in this chunk is `normalize/normalized/tool_results.py`, whose `extract_tool_result_files` (`:57-123`) builds every entry through the private `_build_accessed_file` helper; stamping there covers all five `_READ_TOOLS` and nothing else. The field stays optional and defaults to `None` so the Converse S3 walker, the Gemini GCS walker and any other existing producer keep working unstamped.

Watch the dedup in `normalize/finalize.py:46-60`: it keys on `(name, alg, value)` triples and `provenance` is deliberately **not** part of the key. First-seen wins, vendor entries come first, so a vendor attachment entry that collides with a tool-result entry of the same bytes survives with its own provenance — which is the behaviour we want and which the test pins down.

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/events.py:260-267` (`AIAccessedFile`)
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/normalized/tool_results.py` (`_build_accessed_file`, at the end of the module)
- Test: `shared/tests/normalize/normalized/test_tool_results.py`, `shared/tests/normalize/test_finalize.py`

- [ ] **Step 1: Write the failing producer test** — in `shared/tests/normalize/normalized/test_tool_results.py`, insert one line at the top of the existing `for got, want in zip(...)` body inside `test_extract_tool_result_files`, leaving the rest of the loop as-is (every case in `test_tool_results.yaml` is tool-result-derived, so it holds for all of them):

```python
    for got, want in zip(out, expected, strict=True):
        assert got.provenance == "tool_result"  # <-- new line
        assert got.name == want.name
        ...
```

- [ ] **Step 2: Write the failing dedup test** — append to `shared/tests/normalize/test_finalize.py`:

```python
def test_finalize_stamps_tool_result_provenance() -> None:
    n = _invocation_with_read("/tmp/x.txt", "hello world")
    finalize(n, config=_config())
    assert n.accessed_files[0].provenance == "tool_result"


def test_finalize_dedup_ignores_provenance_and_keeps_first_seen() -> None:
    """The dedup key is (name, alg, value) — provenance is not part of it.
    A vendor attachment entry comes first and survives with its own
    provenance; the colliding tool-result entry is dropped, not merged."""
    import hashlib as _h

    n = _invocation_with_read("/tmp/x.txt", "hello")
    n.accessed_files.append(
        AIAccessedFile(
            name="/tmp/x.txt",
            content_hashes={"sha256": _h.sha256(b"hello").hexdigest()},
            byte_length=5,
            provenance="attachment",
        )
    )
    finalize(n, config=_config())
    assert len(n.accessed_files) == 1
    assert n.accessed_files[0].provenance == "attachment"
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd shared && uv run pytest tests/normalize/normalized/test_tool_results.py tests/normalize/test_finalize.py -v`

Expected: the `test_extract_tool_result_files` cases fail with `AttributeError: 'AIAccessedFile' object has no attribute 'provenance'`; `test_finalize_stamps_tool_result_provenance` fails the same way; `test_finalize_dedup_ignores_provenance_and_keeps_first_seen` fails earlier, at construction — `ValidationError: … provenance / Extra inputs are not permitted` (`_WireModel` is `extra="forbid"`).

- [ ] **Step 4: Implement the field** — in `events.py`, extend `AIAccessedFile` (`:260-267`):

```python
class AIAccessedFile(_WireModel):
    """spec/openapi.yaml — AIAccessedFile.

    ``provenance`` separates the two kinds of entry so a reader can
    replace the ``attachment`` group from a compliance file listing
    without touching ``tool_result`` entries. ``None`` on producers that
    predate the field (the Converse S3 walker, the Gemini GCS walker).
    A third value, ``generated`` — files the model wrote through tool
    use — is reserved and deliberately not emitted: no surface reveals
    that a tool wrote a file, so nothing can set it yet.
    """

    name: str | None = None
    content_hashes: dict[str, str] | None = None
    media_type: str | None = None
    byte_length: int | None = None
    redacted_content: str | None = None
    provenance: Literal["tool_result", "attachment"] | None = None
```

- [ ] **Step 5: Implement the stamp** — in `tool_results.py`, in `_build_accessed_file`'s return:

```python
    return AIAccessedFile(
        name=name,
        content_hashes=content_hashes,
        media_type=media_type,
        byte_length=byte_length,
        redacted_content=redacted,
        provenance="tool_result",
    )
```

`_build_accessed_file` is private to this module and called from exactly one place, so the stamp is unconditional — every entry this extractor yields is by definition a tool read.

- [ ] **Step 6: Run to verify it passes**

Run: `cd shared && uv run pytest -q && uv run ty check`

Expected: all pass. Nothing in `shared/`, `bedrock/` or `vertex/` compares a whole `AIAccessedFile` by equality (checked: the existing assertions are field-by-field, and `redact_for_logging`'s dict comparison at `tests/test_events.py:1124` operates on a hand-built dict, not a model dump), so a new `None`-defaulting field breaks nothing.

- [ ] **Step 7: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/events.py \
        shared/src/slashid_ai_forwarder_core/normalize/normalized/tool_results.py \
        shared/tests/normalize/normalized/test_tool_results.py \
        shared/tests/normalize/test_finalize.py
git commit -m "feat(shared): AIAccessedFile.provenance separates tool reads from uploads"
```

### Task 2.6: `conversation_id` on `EventEnvelope`

`AIInvocationObservedV1` already carries `conversation_id` (`events.py:301`), but `EventEnvelope` (`:307-343`) does not — so `build_event_from_normalized` (`:517-565`) has nothing to read and never sets it, and neither does `build_sparse_event` (`:568-600`). The design requires it on every Anthropic event (mapped from the frame's `session_id`), and it is what groups a denial incident: "same `conversation_id` plus the same `accessed_files` digests is one incident." Adding it to the envelope rather than patching the built event afterwards keeps the Bedrock and Vertex paths byte-identical — they simply leave it unset.

Every existing `EventEnvelope(...)` construction uses keyword arguments (`bedrock/src/slashid_bedrock_forwarder/event_envelope.py:108`, `vertex/src/slashid_vertex_forwarder/event_envelope.py:112` and `:152`, `shared/tests/test_events.py:112`, `:899`, `:953`, `:1239`) — checked, none positional — so field placement is free. Put it directly after `user_agent`, mirroring the order on `AIInvocationObservedV1`.

**Files:**
- Modify: `shared/src/slashid_ai_forwarder_core/events.py:307-343` (`EventEnvelope`), `:542-565` (`build_event_from_normalized`), `:591-600` (`build_sparse_event`)
- Test: `shared/tests/test_events.py`

- [ ] **Step 1: Write the failing test** — append to `shared/tests/test_events.py`:

```python
async def test_envelope_conversation_id_reaches_the_event() -> None:
    """The envelope owns conversation_id; the shared builder carries it
    through unchanged. Bedrock and Vertex leave it unset and get None."""
    from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation

    env = EventEnvelope(
        request_id="r",
        timestamp="2026-09-18T00:00:00Z",
        identity_details=GCPIdentityDetails(),
        model=AIModel(id="claude-sonnet-5"),
        parsed_as="anthropic-inference-hook",
        conversation_id="00000006-0000-4000-8000-000000000000",
    )
    event = await build_event_from_normalized(NormalizedInvocation(), env, config=_config())
    assert event.conversation_id == "00000006-0000-4000-8000-000000000000"


def test_sparse_event_carries_conversation_id() -> None:
    """The compliance reader emits standalone denial events through the
    sparse builder; grouping an incident needs the conversation id there
    too."""
    env = _sparse_envelope()
    assert env.conversation_id is None
    event = build_sparse_event(env, config=_config())
    assert event.conversation_id is None

    env_with = env.model_copy(update={"conversation_id": "sess_1"})
    assert build_sparse_event(env_with, config=_config()).conversation_id == "sess_1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd shared && uv run pytest tests/test_events.py -k conversation_id -v`

Expected: 2 failures.
- `test_envelope_conversation_id_reaches_the_event` — `ValidationError: 1 validation error for EventEnvelope / conversation_id / Extra inputs are not permitted` (`_WireModel` is `extra="forbid"`)
- `test_sparse_event_carries_conversation_id` — `AttributeError: 'EventEnvelope' object has no attribute 'conversation_id'`

- [ ] **Step 3: Implement** — in `events.py`, add the field to `EventEnvelope` immediately after `user_agent`:

```python
    user_agent: str | None = None
    # The vendor's own identifier for the multi-turn conversation this
    # invocation belongs to (Anthropic: the frame's ``session_id``).
    # Envelope-owned rather than derived from the normalized shape: no
    # conversation body carries it. Bedrock and Vertex leave it None.
    conversation_id: str | None = None
```

then carry it through in both builders — in `build_event_from_normalized`'s return, after `user_agent=envelope.user_agent,`:

```python
        conversation_id=envelope.conversation_id,
```

and the identical line in `build_sparse_event`'s return, after its own `user_agent=envelope.user_agent,`.

- [ ] **Step 4: Run to verify it passes**

Run: `cd shared && uv run pytest -q && uv run ty check`

Expected: all pass. Then confirm the sibling forwarders are untouched: `cd bedrock && uv run pytest -q` and `cd vertex && uv run pytest -q` — both still pass, with `conversation_id` defaulting to `None` through their envelopes.

- [ ] **Step 5: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/events.py shared/tests/test_events.py
git commit -m "feat(shared): conversation_id on EventEnvelope, carried into both builders"
```

---

---

## Chunk 3: The frame — parsing, the three-way split, and event assembly

Everything the receiver knows comes out of one POST body, so this chunk turns that body into two things: a `PromptFrame` that never rejects a shape it has not seen before, and the partial `AIInvocationObservedV1` a pending record is made of. The rule that shapes both is the design's "Attribution runs one round behind": frame N carries `[… U(n-1), A(n-1), U(n)]`, and the record it emits names the **previous** run `A(n-1)`. So `input` is the transcript truncated **before** the trailing assistant run, and `used_tools` and `accessed_files` are the round that run consumed — `U(n-1)`, not the fresh `U(n)`. The fresh round drives the verdict and is what the tail record holds; it must contribute nothing to the emitted event. Getting that wrong is silent: an attachment in `U(n)` would stamp a file on a run that never saw it, which is 16 of 17 measured claude.ai frames, and a `Read` result in `U(n)` would land a round early. The shared helpers scope themselves to the last round of whatever transcript they are handed, so handing the builder the truncated one moves attribution back a round without touching them — the fix is a call site, not a helper. `output` and `stop_reason` come from that same trailing run, which the frame carries in full, so this chunk attaches them here rather than deferring them: the design's claim that **a frame-built record is complete on arrival except for attachment digests** is what justifies the whole ownership split — the hook owns every invocation it sees, and the reader only covers joinable runs the hook missed. A frame path that left `output` unset would make every record incomplete and that claim false. `record.py` is storage shape and the control envelope; transcript semantics belong where the transcript is parsed. The record's content address is **not** computed here — it belongs with the store, in a later chunk — so `partial_event` takes the `request_id` it is given. The chunk opens by moving `signature.py` and `capture.py` into the `hook/` subpackage the spec's module layout names, so the new modules land beside them rather than next to `store.py` and the compliance readers.

### Task 3.1: move `signature.py` and `capture.py` into `hook/`

A pure move. Both modules are hook-side by definition — Standard Webhooks verification and raw-frame capture — and the spec's layout puts them under `hook/` beside `frame.py`, `envelope.py` and the four verdict modules Chunk 4 adds. Doing it now means nothing in this chunk or the next imports a path that is about to change. There are exactly two import sites, both verified: `main.py:18` (`from .capture import Capture, GcsCapture`) and `main.py:20` (`from .signature import verify`), plus `anthropic/tests/test_signature.py:10` (`from slashid_anthropic_forwarder.signature import verify`). Nothing else in the repo references either module — `deploy/dev-deploy.sh` mentions capture only in a comment and in the bucket name.

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/hook/__init__.py` (empty)
- Move: `anthropic/src/slashid_anthropic_forwarder/signature.py` → `hook/signature.py`
- Move: `anthropic/src/slashid_anthropic_forwarder/capture.py` → `hook/capture.py`
- Modify: `anthropic/src/slashid_anthropic_forwarder/main.py:18,20`
- Modify: `anthropic/tests/test_signature.py:10`

- [ ] **Step 1: Record the baseline** — `cd anthropic && uv run pytest -q`. Expected: `29 passed`. This is the number the move must not change.

- [ ] **Step 2: Move the two modules**

```bash
cd anthropic/src/slashid_anthropic_forwarder
mkdir -p hook && touch hook/__init__.py
git mv signature.py hook/signature.py
git mv capture.py hook/capture.py
```

- [ ] **Step 3: Run to verify it fails** — `cd anthropic && uv run pytest -q`. Expected: two collection errors, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.capture'` (from `test_main.py` importing `main`) and `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.signature'` (from `test_signature.py`).

- [ ] **Step 4: Fix the three imports** — in `main.py`, `from .capture import ...` becomes `from .hook.capture import Capture, GcsCapture` and `from .signature import verify` becomes `from .hook.signature import verify`. In `tests/test_signature.py:10`, `slashid_anthropic_forwarder.signature` becomes `slashid_anthropic_forwarder.hook.signature`. Nothing else changes — not the module bodies, not a single test assertion.

- [ ] **Step 5: Run to verify it passes** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest -q && uv run ty check`. Expected: `29 passed`, identical to Step 1.

- [ ] **Step 6: Commit**

```bash
git add -A anthropic/src/slashid_anthropic_forwarder anthropic/tests/test_signature.py
git commit -m "refactor(anthropic): move signature and capture into the hook subpackage"
```

### Task 3.2: `PromptFrame` and `split_transcript`

The envelope tolerates unknown fields and unknown discriminator values — the protocol grows by addition, and rejecting a delivery over something new is a webhook failure that counts against Anthropic's circuit breaker. The nine top-level fields are the ones every captured frame carries: `type`, `request_id`, `tenant_id`, `actor`, `source`, `messages`, `session_id`, `model`, `metadata`. `messages` reuse the shared Anthropic schema, which Chunk 2 taught the hook's `tool_name` spelling (Task 2.1) and its `attachment` block (Task 2.3); that schema pins `role` to user/assistant, so a frame with a new role fails to parse and `main.py`'s guard answers allow.

`split_transcript` is the one scan three later paths depend on, and it returns three regions because the design needs all three. The **fresh round** (everything after the last assistant message) is what the model is about to read and what the verdict scans; it is the tail record's content, and it is not part of the record this frame emits. The **trailing assistant run** (the consecutive assistant messages immediately before it) is the previous invocation's answer — the run the emitted record names. **`before`** is everything ahead of that run: the transcript the emitted record's `input` ends with, whose own last round is the one that run consumed. The scan itself is `shared/.../normalize/turn.py::after_last_assistant` (`:34`), not a reimplementation: it is the same rule `extract_tool_result_files` and `events.py::_used_tools` already apply one layer down, which is exactly why handing them `before` moves attribution back a round for free — and why a second copy that drifted would silently re-attribute files.

The field is named `fresh`, not `consumed`, because "consumed" is the word the bug hides behind: `U(n)` is consumed by the invocation this frame is about to feed, while the record being emitted is attributed to the round `A(n-1)` consumed, one earlier.

Two fixture shapes make the split non-trivial, and both are in the table. `frame_mcp_tool.json` carries **two consecutive user messages** — a `tool_result` message followed by a separate `"Tool loaded."` user message, which is how deferred-tool loading looks — so a round is not always one message. And under enforcement a denial prevents an assistant turn, so a post-denial transcript has two consecutive user-role runs with nothing between them; `frame_after_shadow_deny.json` is the *shadow*-mode capture of that scenario, where the request ran and the assistant did answer (`pong-F1`), so the enforced shape is produced in the table by appending one user message to it.

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/hook/frame.py`
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

from slashid_anthropic_forwarder.hook.frame import PromptFrame, Source, split_transcript

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> PromptFrame:
    return PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))


def test_tool_result_frame_parses_with_the_shared_blocks() -> None:
    frame = load("frame_tool_result")
    assert frame.type == "prompt"
    assert frame.actor.id == "user_01AbCdEfGhIjKlMnOpQrStUv"
    assert frame.source.application == "claude-code"
    assert frame.session_id == "00000002-0000-4000-8000-000000000000"
    # The hook spells it `tool_name`; the alias from Chunk 2 Task 2.1 is why
    # this reads as `.name`.
    use = frame.messages[1].content[1]
    assert isinstance(use, AnthropicToolUseBlock) and use.name == "Read"
    result = frame.messages[2].content[0]
    assert isinstance(result, AnthropicToolResultBlock)
    assert result.tool_use_id == use.id and result.is_error is False


def test_three_attachments_sit_between_two_text_blocks() -> None:
    """frame_attachment.json: one user message, five blocks, and nullable
    metadata on the attachments — the JPEG and the PDF have no file_name,
    the text upload and the PDF no size_bytes."""
    blocks = load("frame_attachment").messages[0].content
    assert [type(b).__name__ for b in blocks] == [
        "AnthropicTextBlock",
        "AnthropicAttachmentBlock",
        "AnthropicAttachmentBlock",
        "AnthropicAttachmentBlock",
        "AnthropicTextBlock",
    ]
    txt, jpeg, pdf = (b for b in blocks if isinstance(b, AnthropicAttachmentBlock))
    assert (txt.file_name, txt.size_bytes) == ("maria.txt", None)
    assert (jpeg.file_name, jpeg.size_bytes, jpeg.text) == (None, 70657, None)
    assert pdf.file_name is None and pdf.size_bytes is None and pdf.text is not None


def test_mcp_frame_keeps_its_two_consecutive_user_messages() -> None:
    frame = load("frame_mcp_tool")
    assert [m.role for m in frame.messages] == [
        "user",
        "assistant",
        "user",
        "user",
        "assistant",
        "user",
    ]
    use = frame.messages[4].content[0]
    assert isinstance(use, AnthropicToolUseBlock) and use.name == "mcp__demo__echo"
    # A server-executed tool's result is a placeholder, not content.
    placeholder = frame.messages[2].content[0]
    assert isinstance(placeholder, AnthropicToolResultBlock)
    assert placeholder.content == "[non-text content]"


def test_failed_tool_result_carries_is_error() -> None:
    result = load("frame_tool_error").messages[2].content[0]
    assert isinstance(result, AnthropicToolResultBlock) and result.is_error is True


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
    assert frame.actor.type == "robot" and frame.actor.id is None
    assert frame.session_id is None and frame.model is None
    assert frame.messages[0].content[0].type == "sparkle"


def test_connection_test_is_recognised() -> None:
    frame = load("frame_first_turn")
    assert not frame.is_connection_test()
    probe = frame.model_copy(update={"source": Source(application="config-test")})
    assert probe.is_connection_test()


@yaml_pytest(filename="test_split_transcript.yaml")
def test_split_transcript(
    fixture: str,
    take: int | None,
    append: list[dict[str, Any]],
    before: int,
    assistant_run: int,
    fresh: int,
) -> None:
    frame = load(fixture)
    messages = list(frame.messages)[:take] + [
        AnthropicRequestMessage.model_validate(m) for m in append
    ]
    split = split_transcript(frame.model_copy(update={"messages": messages}))
    assert (len(split.before), len(split.assistant_run), len(split.fresh)) == (
        before,
        assistant_run,
        fresh,
    )
    # The three parts partition the transcript — nothing dropped, nothing reordered.
    assert split.before + split.assistant_run + split.fresh == messages
```

with `tests/test_split_transcript.yaml`:

```yaml
# No assistant message at all: the whole transcript is fresh, and there is
# no previous run for a record to name.
id: first_turn_is_all_fresh
fixture: frame_first_turn
take: null
append: []
before: 0
assistant_run: 0
fresh: 1
---
id: prompt_answer_result
fixture: frame_tool_result
take: null
append: []
before: 1
assistant_run: 1
fresh: 1
---
id: five_messages_take_only_the_last_assistant_run
fixture: frame_subagent_child
take: null
append: []
before: 3
assistant_run: 1
fresh: 1
---
# Deferred-tool loading: a tool_result message, then a separate "Tool loaded."
# user message, one round for the invocation this frame feeds. Truncated to
# the first four messages, which is exactly the frame that round produced.
id: a_fresh_round_can_span_two_user_messages
fixture: frame_mcp_tool
take: 4
append: []
before: 1
assistant_run: 1
fresh: 2
---
# The same two user messages, one round later: they sit in `before` now, and
# they are the round the record this frame emits is attributed to.
id: the_same_two_user_messages_fall_into_before
fixture: frame_mcp_tool
take: null
append: []
before: 4
assistant_run: 1
fresh: 1
---
id: consecutive_assistant_messages_are_one_run
fixture: frame_first_turn
take: null
append:
  - {role: assistant, content: [{type: text, text: first}]}
  - {role: assistant, content: [{type: text, text: second}]}
  - {role: user, content: [{type: text, text: go on}]}
before: 1
assistant_run: 2
fresh: 1
---
# Shadow mode let the denied request run, so the assistant answered and the
# transcript is ordinary. No special case is needed for it.
id: a_shadow_denial_looks_like_any_other_turn
fixture: frame_after_shadow_deny
take: null
append: []
before: 1
assistant_run: 1
fresh: 1
---
# Under enforcement the denial prevents the assistant turn, so the next
# delivery shows two consecutive user-role runs and a two-message fresh
# round that keeps re-including the offending content.
id: an_enforced_denial_leaves_two_user_runs
fixture: frame_after_shadow_deny
take: null
append:
  - {role: user, content: [{type: text, text: "and now: pong-F3"}]}
before: 1
assistant_run: 1
fresh: 2
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_frame.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.hook.frame'`.

- [ ] **Step 3: Implement** `hook/frame.py`:

```python
"""Wire model for the Inference hooks prompt frame, and the transcript split.

The envelope tolerates unknown fields and unknown discriminator values:
the protocol grows by addition, and rejecting a delivery over something
new is a webhook failure. ``messages`` reuse the shared Anthropic schema
— a hook transcript is the Messages API content model plus attachments.
That schema pins ``role`` to user/assistant, so a frame carrying a new
role fails to parse and ``main.py``'s guard answers allow.
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
    # Both id and email_address are documented nullable.
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
        circuit breaker's recovery checks. Carries no user content, so it
        bypasses the checks and writes no record."""
        return self.source.application == "config-test"


@dataclass(frozen=True)
class Split:
    """``[… before …][ trailing assistant run ][ fresh ]``.

    ``fresh`` is everything after the last assistant message: what the
    model is about to read, and what the verdict scans. It is the tail
    record's content and it contributes nothing to the record this frame
    emits — attribution runs one round behind. It can span several
    messages — deferred-tool loading appends a user message after a
    tool_result, and an enforced denial leaves two user runs in a row.

    ``assistant_run`` is the last run of consecutive assistant messages,
    which is one response however many messages it arrived as. It is the
    previous invocation's answer — the run the emitted record names, and
    the record's ``output`` — and it is empty on a first turn, which is a
    frame with no previous invocation to report at all.

    ``before`` is everything ahead of that run: the transcript the
    emitted record's ``input`` ends with. Its own last round is the one
    that run consumed, which is why handing it to the shared helpers
    attributes ``used_tools`` and ``accessed_files`` correctly.
    """

    before: list[AnthropicRequestMessage]
    assistant_run: list[AnthropicRequestMessage]
    fresh: list[AnthropicRequestMessage]


def split_transcript(frame: PromptFrame) -> Split:
    """Partition a frame's transcript. The scan is ``after_last_assistant``
    rather than a local copy: the same rule decides attribution inside
    ``extract_tool_result_files`` and ``events.py::_used_tools``, and a
    second implementation that drifted would re-attribute files."""
    messages = frame.messages
    fresh = list(after_last_assistant(messages))
    head = messages[: len(messages) - len(fresh)]
    start = len(head)
    while start > 0 and head[start - 1].role == "assistant":
        start -= 1
    return Split(before=head[:start], assistant_run=head[start:], fresh=fresh)
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_frame.py -v && uv run ty check`. Expected: 14 passed (6 plain + 8 yaml cases), ruff and ty clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/hook/frame.py \
        anthropic/tests/test_frame.py anthropic/tests/test_split_transcript.yaml
git commit -m "feat(anthropic): prompt-frame envelope and the three-way transcript split"
```

### Task 3.3: `accessed_files_for` — one recipe for the verdict and the record

The files a round puts in front of the model come from two places, and both paths need the identical answer. The verdict calls this on the **fresh** round before deciding (Chunk 4's preflight takes `list[AIAccessedFile]`); the record calls it one round behind, on the round the previous run consumed. Those are the same files a frame apart — what the verdict judged in frame N is what frame N+1's record reports — so a second recipe would let a request be allowed on one digest and recorded under another.

Which round is not this module's decision. Every function here scans the **last round of the messages it is handed**, and the caller picks the slice: the verdict hands it `frame.messages` and lands on the fresh round, the record hands it `split.before` and lands one round earlier. That is the whole mechanism behind the attribution rule, and it is why neither the shared helpers nor this module needs a mode flag.

**Tool results** are already handled in the spine — `extract_tool_result_files` walks the canonical messages, matches the `_READ_TOOLS` table, strips Claude Code's `cat -n` prefixes, skips `is_error` results (the content is an error string, not file bytes) and scopes itself with `after_last_assistant` to the last round of what it is given. So this module normalizes the transcript with the public `message_to_normalized_invocation` and lets `finalize` do that half.

**Attachments** are new. A hook attachment carries extracted text, never bytes, so the digest is over that text and `byte_length` is its length — not the frame's `size_bytes`, which describes the upload and disagrees whenever the stored copy was processed. An attachment with no text (the JPEG in `frame_attachment.json`) yields no entry at all: there is nothing to hash. Names are the awkward part: `file_name` is null for images by documented behaviour and was null for this tenant's PDFs too, and claude.ai lists the uploads in a `<uploaded_files>` text block whose order does **not** match the block order — the fixture lists `guiaSADT.pdf`, the JPEG, then `maria.txt` for blocks ordered text, JPEG, PDF. So a nameless block claims a listed name by media type, consuming it so two same-typed uploads cannot both claim one entry. Every entry is stamped `provenance="attachment"` (Chunk 2 Task 2.5), which is what later lets Reader B replace the attachment group from a compliance listing without touching tool-result entries.

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/hook/envelope.py`
- Test: `anthropic/tests/test_envelope.py`, `anthropic/tests/test_accessed_files_for.yaml`

- [ ] **Step 1: Write the failing tests**

```python
"""Frame → the partial event a pending record is made of."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Literal

from pydantic import BaseModel
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.envelope import accessed_files_for, attachment_files
from slashid_anthropic_forwarder.hook.frame import PromptFrame
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
```

with `tests/test_accessed_files_for.yaml`:

```yaml
id: read_result_is_hashed_after_the_line_numbers_are_stripped
fixture: frame_tool_result
expected:
  - name: /home/alice/proj/notes.txt
    sha256: e6bab19e50f90145e62b963a3584ec72ea6b88adcb71de6522301ebb9fa0813d
    media_type: text/plain
    byte_length: 49
---
# The content of a failed Read is an error message, not file bytes.
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
# The txt and the pdf carry extracted text; the jpeg carries none, so it is
# not an entry. byte_length is the length of that text, never size_bytes.
id: attachments_with_text_are_hashed_and_named
fixture: frame_attachment
expected:
  - name: maria.txt
    sha256: 576f5772eb89115f882ac39a431b48a4bc1872d84b48573ee1dd5c1f43065055
    media_type: text/plain
    byte_length: 27
    provenance: attachment
  - name: guiaSADT.pdf
    sha256: 3f6af091f4b9191b99ff17468d46aee5079b69c13ad754979846b6883e92b5ff
    media_type: application/pdf
    byte_length: 47
    provenance: attachment
---
# Handed the whole frame, the scan lands on the fresh round — the verdict's
# slice. The Bash result sits a round earlier and is outside it; only the
# Read is hashed. Task 3.4 hands the same function `split.before` and gets
# the other round.
id: only_the_last_round_counts
fixture: frame_subagent_child
expected:
  - {name: /home/alice/proj/a.txt, sha256: 49e5ab7eb50b84ab791a464363506fca587318ec5400cfe9d9f08ef43f22103a}
---
# An MCP tool result is not a read, and a server tool's result is a
# placeholder anyway — nothing hashable in either.
id: mcp_results_are_not_files
fixture: frame_mcp_tool
expected: []
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_envelope.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.hook.envelope'`.

- [ ] **Step 3: Implement** `hook/envelope.py` (this task's half; the event builder is Task 3.4):

```python
"""Frame → the partial ``AIInvocationObservedV1`` a pending record holds.

Attribution runs one round behind. Frame N carries ``[… U(n-1), A(n-1),
U(n)]`` and the record names ``A(n-1)``, so ``input`` is the transcript
truncated before that run and ``used_tools`` / ``accessed_files`` are
the round it consumed, ``U(n-1)``. The fresh round ``U(n)`` drives the
verdict and belongs to the tail record; it is not part of this one.

Nothing here decides which round that is. Every function scans the last
round of the messages it is handed, so the caller chooses the boundary
by choosing the slice. ``output`` and ``stop_reason`` are the trailing
run's, merged into one response by ``partial_event``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import UTC, datetime

from slashid_ai_forwarder_core.content_utils import truncate_middle
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    message_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicRequestMessage,
    AnthropicTextBlock,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.normalized.media_types import parse_media_type
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant

from ..config import Config

PARSED_AS = "anthropic-inference-hook"

# claude.ai lists uploads as ``<file_path>/mnt/user-data/uploads/<name></file_path>``
# inside a text block ahead of the attachment blocks. The listed order does
# not match the block order, so a nameless block is paired by media type.
_UPLOAD_PATH = re.compile(r"<file_path>(.*?)</file_path>")


def _uploaded_names(messages: list[AnthropicRequestMessage]) -> list[str]:
    names: list[str] = []
    for msg in messages:
        for block in msg.content:
            if isinstance(block, AnthropicTextBlock) and "<uploaded_files>" in block.text:
                names.extend(p.rsplit("/", 1)[-1] for p in _UPLOAD_PATH.findall(block.text))
    return names


def _name_for(block: AnthropicAttachmentBlock, candidates: list[str]) -> str | None:
    """Consume a listed name for this block. Consuming matters: two uploads
    of the same media type must not both claim the first listed name."""
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
    """One entry per text-bearing attachment in the last round of ``messages``.

    Which round that is belongs to the caller: the whole frame scans the
    fresh round, ``split.before`` scans the round the previous run
    consumed.

    A hook attachment carries extracted text and never bytes, so the
    digest and ``byte_length`` are over that text — not over the frame's
    ``size_bytes``, which describes the upload and disagrees with it
    whenever Claude stored a processed copy. An attachment with no text
    (an image) yields no entry: there is nothing to hash.
    """
    last_round = list(after_last_assistant(messages))
    candidates = _uploaded_names(last_round)
    out: list[AIAccessedFile] = []
    for msg in last_round:
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
                    media_type=parse_media_type(block.media_type),
                    byte_length=len(data),
                    redacted_content=(
                        truncate_middle(block.text, config.max_content_size)
                        if config.include_raw_content
                        else None
                    ),
                    provenance="attachment",
                )
            )
    return out


async def _normalized(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> NormalizedInvocation:
    """Canonicalize the messages it is given, with an empty response.

    Empty because this path only needs the file side, and the round it
    is scanning has not been answered. Task 3.4 gives the function the
    run that answered. ``finalize`` then adds the last round's
    tool-result files behind the attachment entries and dedupes the
    union, first-seen wins.
    """
    normalized = await message_to_normalized_invocation(
        AnthropicRequestBody(messages=messages),
        AnthropicMessage(type="message", role="assistant", content=[]),
        config=config,
    )
    normalized.accessed_files = attachment_files(messages, config=config)
    return finalize(normalized, config=config)


async def accessed_files_for(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> list[AIAccessedFile]:
    """Files the last round of ``messages`` puts in front of the model.

    The verdict passes the whole frame and checks the fresh round before
    answering; the record passes ``split.before`` and reports the round
    the previous run consumed. One recipe either way, so the file a
    verdict allowed cannot be recorded under a different digest.
    """
    return (await _normalized(messages, config=config)).accessed_files


def signed_at_iso(signed_at: int) -> str:
    """The attested ``webhook-timestamp`` as the wire's timestamp."""
    return datetime.fromtimestamp(signed_at, tz=UTC).isoformat()
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_envelope.py -v && uv run ty check`. Expected: 8 passed (6 yaml cases + 2 plain). `signed_at_iso` is unused until Task 3.4 — it is public, so `ruff --fix` leaves it alone.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/hook/envelope.py \
        anthropic/tests/test_envelope.py anthropic/tests/test_accessed_files_for.yaml
git commit -m "feat(anthropic): accessed-files recipe shared by the verdict and the record"
```

### Task 3.4: `partial_event` — the record the frame can build

What the frame supplies is fixed by the design's field mapping: identity (`{kind: "anthropic", user_id: actor.id}`, and a null `actor.id` drops the event because the server rejects an identity with no identifier), `model` (`AIModel(id=model or "unknown", provider="anthropic", raw_model_id=model)`), `timestamp` from the attested `webhook-timestamp`, `conversation_id` from `session_id`, `user_agent` from `source.application`, `input` ending before the trailing assistant run, the `used_tools` and `accessed_files` of the round that run consumed, and `available_tools`/`available_tool_servers` synthesized from observed `tool_use` names. Tokens are zero; no surface carries usage.

The one decision this function makes is which slice it hands down, and the design names the trap: `after_last_assistant()` returns the *fresh* round and drives the verdict, but the record names the previous run, so attribution needs the boundary one round earlier. `build_event_from_normalized` owns the input hashing and the tool-result→tool_use join, and its `_used_tools` scopes itself to the round after the last assistant message **of the transcript it is given** — so it is given `split.before`, whose last round is the one `A(n-1)` consumed, and both halves of the attribution fall out unchanged. Handing it `frame.messages` instead is the bug: `U(n)`'s `Read` result would land on the run that merely requested it, a round early, and `U(n)`'s attachment block would land on a run that never saw the file.

`build_tools_declared` turns observed names into canonical tools and servers. The names come from `split.before` **plus the trailing run** — this record's own transcript and the answer it names — and never from the fresh round, whose tool calls belong to the tail. It is not cosmetic: without a `tools_declared` entry whose `(server, name)` key matches, `_used_tools` cannot map a result to a tool id and drops the entry. And the names must be distinct before they get there, because `build_tools_declared` dedupes servers but not tools.

Two shapes return `None` rather than a record. A null `actor.id`, as above. And a frame with **no trailing assistant run** — a first turn — because there is no previous invocation to name; its fresh round is not lost, it is what the tail record holds.

**The answer is attached here, not deferred.** The design's claim that a frame-built record is complete on arrival except for attachment digests is what justifies the ownership split, so a frame path that left `output` unset would falsify it — and `A(n-1)` is sitting in `split.assistant_run` in this very frame, so deferring buys nothing. Three details. A run can arrive as **several assistant messages**, which is one answer delivered in pieces, so their content blocks concatenate in transcript order into a single response. The blocks are also **filtered to the response-side union** — `AnthropicRequestContentBlock` is a superset that adds `tool_result` and (Chunk 2 Task 2.3) `attachment`, neither of which an assistant turn carries, and `AnthropicMessage.content` will not validate them; unknown blocks go too, which changes nothing because `_message_to_output` already skips them. And `stop_reason` is `tool_use` when the run's last block is a tool call, `end_turn` otherwise. `guardrail_intervened` is **not** set here: it belongs to the denial path in a later chunk, and it comes from the verdict actually answered rather than from block shape.

That goes in through the normalized invocation rather than by patching the built event: `message_to_normalized_invocation` walks the response into `normalized.output` and maps `stop_reason` through `STOP_REASONS` (`tool_use` and `end_turn` are both in `AIStopReason`, verified in `shared/.../events.py:41`), and `build_event_from_normalized` hashes that half and reads `normalized.output.stop_reason` straight off it. So the earlier draft's `model_copy(update={"output": None, ...})` disappears, and with it the reason it existed — `NormalizedInvocationOutput` has no absent state, so an empty response would have hashed `{"stop_reason": "unknown"}` into a meaningless `output`.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/hook/envelope.py` (imports; `_normalized` takes the answer; new builder at the end)
- Test: `anthropic/tests/test_envelope.py`, `anthropic/tests/test_partial_event.yaml`

- [ ] **Step 1: Write the failing tests** — extend the `hook.envelope` import to add `PARSED_AS`, `partial_event` and `signed_at_iso`, add an `AnthropicRequestMessage` import from `slashid_ai_forwarder_core.normalize.anthropic.schema` (the spliced messages are typed), then append:

```python
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
        config=config(include_raw_content=True),
    )
    assert event is not None and event.input is not None and event.output is not None
    body = event.input.redacted_text or ""
    assert "wc -l /home/alice/proj/a.txt" in body  # the round the run consumed
    assert "toolu_01UbdhcQRwkxR8JFoAGZi2i9" not in body  # the trailing run itself
    assert "alpha line one" not in body  # the fresh round, which is the tail's
    # That run is not missing, it is the answer.
    assert "toolu_01UbdhcQRwkxR8JFoAGZi2i9" in (event.output.redacted_text or "")


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
```

adding `AIInvocationObservedV1` and `AIStopReason` to the `slashid_ai_forwarder_core.events` import, with `tests/test_partial_event.yaml`:

```yaml
# An opening prompt has no previous run to name, so there is no record to
# build. Its fresh round is not lost: it is what the tail record holds.
id: a_first_frame_has_no_previous_run
fixture: frame_first_turn
insert_at: null
append: []
expected: null
---
# The Read result is in the fresh round, so it belongs to the invocation this
# frame is about to feed — not to the run that asked for it. The tool is still
# declared: the trailing run issued it.
id: a_fresh_read_result_is_not_attributed_yet
fixture: frame_tool_result
insert_at: null
append: []
expected:
  stop_reason: tool_use
  tool_names: [Read]
  servers: [[builtin, runtime]]
---
# The run that answers this record ends in two parallel tool calls, so they
# are one output and the stop reason is tool_use. Both results sit in the
# fresh round and land on the next record, not this one.
id: a_run_ending_in_parallel_tool_calls_stops_on_tool_use
fixture: frame_multi_tool
insert_at: null
append: []
expected:
  stop_reason: tool_use
  tool_names: [Read]
  servers: [[builtin, runtime]]
---
# The plainest shape there is: a prompt, a text answer, a new prompt. Nothing
# consumed, nothing declared, and the answer stops on end_turn.
id: a_text_only_run_stops_on_end_turn
fixture: frame_first_turn
insert_at: null
append:
  - {role: assistant, content: [{type: text, text: "Hello — what would you like to do?"}]}
  - {role: user, content: [{type: text, text: nothing yet}]}
expected: {}
---
# Continue the same conversation one round: the answer becomes the trailing
# run, the Read result becomes the round it consumed, and the file lands.
id: the_read_lands_one_round_later
fixture: frame_tool_result
insert_at: null
append:
  - {role: assistant, content: [{type: text, text: The first line is SCENARIO-B notes.}]}
  - {role: user, content: [{type: text, text: thanks}]}
expected:
  used_tool_ids: [toolu_01Dqhr2d1w2UCUqbXhCSGutC]
  used_tool_errors: [false]
  accessed_files:
    - {name: /home/alice/proj/notes.txt, sha256: e6bab19e50f90145e62b963a3584ec72ea6b88adcb71de6522301ebb9fa0813d}
  tool_names: [Read]
  servers: [[builtin, runtime]]
---
# A failed Read is still a tool the model used; it is just not a file read.
id: a_failed_read_is_a_used_tool_but_not_a_file
fixture: frame_tool_error
insert_at: null
append:
  - {role: assistant, content: [{type: text, text: missing}]}
  - {role: user, content: [{type: text, text: ok}]}
expected:
  used_tool_ids: [toolu_01GSt4gp6UqW6a5RsxRvyn8d]
  used_tool_errors: [true]
  tool_names: [Read]
  servers: [[builtin, runtime]]
---
# Deferred-tool loading: the ToolSearch result and the separate "Tool loaded."
# message are one round, and it is the round the trailing run consumed. That
# run's own tool_use is mcp__demo__echo, which is what yields the demo MCP
# server; its result sits in the fresh round and is not attributed here.
id: the_attributed_round_spans_two_user_messages
fixture: frame_mcp_tool
insert_at: null
append: []
expected:
  stop_reason: tool_use
  used_tool_ids: [toolu_013a8rkKEU8XmKMJNmwAkcXn]
  used_tool_errors: [false]
  tool_names: [ToolSearch, echo]
  servers: [[builtin, runtime], [demo, mcp]]
---
# The rule, pinned. Give the attachment frame a previous round and its three
# attachment blocks stay in the fresh one, which belongs to the tail — so this
# event carries no file at all. Handing the builder the whole frame instead
# would stamp maria.txt and guiaSADT.pdf on a run that never saw either, on
# 16 of the 17 measured claude.ai frames.
id: an_attachment_in_the_fresh_round_is_not_on_this_record
fixture: frame_attachment
insert_at: 0
append:
  - {role: user, content: [{type: text, text: oi}]}
  - {role: assistant, content: [{type: text, text: "Olá! Como posso ajudar?"}]}
expected: {}
---
# The complement: one round later the same attachments are the round the
# trailing run consumed, and both text-bearing ones land. The JPEG carries no
# text either way, so it is never an entry.
id: attachments_land_once_their_round_is_the_consumed_one
fixture: frame_attachment
insert_at: null
append:
  - {role: assistant, content: [{type: text, text: "Recebi os anexos."}]}
  - {role: user, content: [{type: text, text: "e agora?"}]}
expected:
  accessed_files:
    - {name: maria.txt, sha256: 576f5772eb89115f882ac39a431b48a4bc1872d84b48573ee1dd5c1f43065055, provenance: attachment}
    - {name: guiaSADT.pdf, sha256: 3f6af091f4b9191b99ff17468d46aee5079b69c13ad754979846b6883e92b5ff, provenance: attachment}
---
# The Bash result is what the trailing Read run consumed, and Bash is no file
# read, so there is no file. The fresh Read result — a.txt — is the next
# invocation's. Both names are declared: the transcript and the run that
# answered it reveal them.
id: only_the_round_the_previous_run_consumed_is_attributed
fixture: frame_subagent_child
insert_at: null
append: []
expected:
  stop_reason: tool_use
  used_tool_ids: [toolu_01SpA8HaR3QvRatgbe8q11GT]
  used_tool_errors: [false]
  tool_names: [Bash, Read]
  servers: [[builtin, runtime]]
---
# A shadow-mode denial needs no special case: the request ran, the assistant
# answered, and this is an ordinary record.
id: a_shadow_denied_turn_is_an_ordinary_invocation
fixture: frame_after_shadow_deny
insert_at: null
append: []
expected: {}
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_envelope.py -v`. Expected: collection ERROR, `ImportError: cannot import name 'partial_event' from 'slashid_anthropic_forwarder.hook.envelope'`.

- [ ] **Step 3: Implement** — `_normalized` keeps scanning whatever slice it is handed; it only learns to take the run that answered it. Its signature and first statement become:

```python
async def _normalized(
    messages: list[AnthropicRequestMessage],
    *,
    answer: AnthropicMessage | None = None,
    config: Config,
) -> NormalizedInvocation:
    normalized = await message_to_normalized_invocation(
        AnthropicRequestBody(messages=messages),
        answer or AnthropicMessage(type="message", role="assistant", content=[]),
        config=config,
    )
```

with the docstring's second sentence becoming "Empty on the verdict's path, which is judging a round nothing has answered; `partial_event` passes the trailing run." Then append the three functions:

```python
def _tool_names(messages: list[AnthropicRequestMessage]) -> list[str]:
    """Distinct raw tool names, first-seen order. Distinct matters:
    ``build_tools_declared`` dedupes servers but not tools."""
    seen: dict[str, None] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, AnthropicToolUseBlock):
                seen.setdefault(block.name)
    return list(seen)


def _answer(run: list[AnthropicRequestMessage]) -> AnthropicMessage:
    """The trailing assistant run as the one response it is.

    A run can arrive as several assistant messages — one answer in
    pieces, not several answers — so the blocks concatenate in
    transcript order. The filter is the two content unions: the
    request side adds ``tool_result`` and ``attachment``, which an
    assistant turn never carries and ``AnthropicMessage`` will not
    validate, and unknown blocks go with them because
    ``_message_to_output`` skips those anyway.

    ``stop_reason`` follows the run's last block: a tool call means the
    model stopped to call it, anything else means it finished talking.
    ``guardrail_intervened`` is not decided here — it belongs to the
    denial path, and it comes from the verdict that was answered rather
    than from the shape of a block.
    """
    blocks = [
        block
        for msg in run
        for block in msg.content
        if isinstance(block, AnthropicTextBlock | AnthropicToolUseBlock | AnthropicThinkingBlock)
    ]
    stopped_to_call = bool(blocks) and isinstance(blocks[-1], AnthropicToolUseBlock)
    return AnthropicMessage(
        type="message",
        role="assistant",
        content=blocks,
        stop_reason="tool_use" if stopped_to_call else "end_turn",
    )


async def partial_event(
    frame: PromptFrame,
    *,
    request_id: str,
    signed_at: int,
    config: Config,
) -> AIInvocationObservedV1 | None:
    """The record frame N emits: the invocation its **previous** run answered.

    ``input`` is the transcript truncated before that run, which is what
    makes the shared helpers attribute ``used_tools`` and
    ``accessed_files`` to the round it consumed; ``output`` is the run
    itself. The fresh round drives the verdict and is the tail record's;
    it contributes nothing here. Nothing is left outstanding but the
    attachment digests a compliance listing supplies.

    ``request_id`` is the record's provisional address, computed by the
    addressing module and passed in — the frame cannot derive it, since
    the strong anchor lives in the trailing run this builder only reads
    tool names from. ``None`` when there is no trailing run to name (a
    first turn), and when ``actor.id`` is null: the server rejects an
    Anthropic identity with no identifier, so there is nothing useful to
    store.
    """
    split = split_transcript(frame)
    if not frame.actor.id or not split.assistant_run:
        return None
    normalized = await _normalized(
        split.before, answer=_answer(split.assistant_run), config=config
    )
    # The frame carries no tool definitions, so synthesize them from the
    # names this record's own transcript and its answer reveal — never
    # from the fresh round. Not cosmetic: without a declared tool whose
    # (server, name) key matches, ``_used_tools`` cannot map a result to
    # a tool id and drops the entry.
    tools, servers = build_tools_declared(
        (name, None, None) for name in _tool_names([*split.before, *split.assistant_run])
    )
    normalized.input.tools_declared = tools
    normalized.input.tool_servers = servers
    return await build_event_from_normalized(
        normalized,
        EventEnvelope(
            request_id=request_id,
            timestamp=signed_at_iso(signed_at),
            identity_details=AnthropicIdentityDetails(user_id=frame.actor.id),
            model=AIModel(
                id=frame.model or "unknown", provider="anthropic", raw_model_id=frame.model
            ),
            parsed_as=PARSED_AS,
            user_agent=frame.source.application,
            conversation_id=frame.session_id,
        ),
        config=config,
    )
```

Merge the new names into the existing import block (`ruff --fix` sorts them):

```python
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
    EventEnvelope,
    build_event_from_normalized,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicRequestMessage,
    AnthropicTextBlock,
    AnthropicThinkingBlock,
    AnthropicToolUseBlock,
)
from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared

from .frame import PromptFrame, split_transcript
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_envelope.py -v && uv run ty check`. Expected: 25 passed (8 from Task 3.3, 11 yaml cases and 6 plain here). If `used_tools` comes back empty on a case that expects an entry, the cause is `tools_declared` — the synthesized name did not match the join key, not the attribution rule. If a `stop_reason` comes back `"unknown"`, `_answer` set a string `STOP_REASONS` does not map.

- [ ] **Step 5: Run the whole suite** — `cd anthropic && uv run pytest -q`. Expected: `68 passed` (29 from Chunk 1, 14 from Task 3.2, 25 here).

- [ ] **Step 6: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/hook/envelope.py \
        anthropic/tests/test_envelope.py anthropic/tests/test_partial_event.yaml
git commit -m "feat(anthropic): frame-built record — input, answer and attribution one round behind"
```

---

---

## Chunk 4: Verdict — policy, preflight, composition

The receiver has to answer Anthropic within its verdict timeout, and two independent checks can inform that answer: the Go policy receiver (`POST {SLASHID_POLICY_URL}`, which re-verifies the signature itself) and the preflight content check (`POST {SLASHID_ENDPOINT}/ip/nhi/ai/preflight`). This chunk builds them as four modules under `anthropic/src/slashid_anthropic_forwarder/hook/`, matching the spec's module layout: `checks.py` holds the types both clients return, `policy.py` and `preflight.py` are the clients, and `verdict.py` composes — running both concurrently under one budget, applying `SLASHID_VERDICT_FAIL_MODE` when a check cannot answer, and applying `SLASHID_SHADOW_MODE` last. Shadow mode is on by default (`config.py:43`) and inverts the old `enforce` flag: `shadow_mode=True` means every check still runs and is logged, but allow is answered. Because a denial's record must say `guardrail_intervened` only when a block actually happened, `decide()` returns both the composed verdict and the one actually answered, so the caller persists what went out instead of re-reading shadow mode at flush time. Chunk 5 wires it into `main.py`.

### Task 4.1: `checks.py` — shared verdict types

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/hook/checks.py`
- Create if absent: `anthropic/src/slashid_anthropic_forwarder/hook/__init__.py` (empty; `frame.py`, `signature.py` and `capture.py` live in this subpackage too)

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
    # Which check decided: "policy", "preflight", "marker", "fail_mode",
    # "bypass", "shadow" (shadow mode answered for it), "none".
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


@dataclass(frozen=True)
class Decision:
    """What the checks composed, and what the receiver actually answered.

    They differ only under shadow mode. The pending record stores
    ``answered``: a flush can run an hour later, across a redeploy or a
    mixed-revision rollout, so re-reading shadow mode then would describe a
    configuration that never applied to this call.
    """

    composed: Verdict
    answered: Verdict

    @property
    def blocked(self) -> bool:
        """True only when a deny actually went back to Anthropic."""
        return self.answered.denied
```

### Task 4.2: `policy.py` — forward the raw frame to the Go receiver

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/hook/policy.py`, `hook/checks.py` (above)
- Test: `anthropic/tests/test_policy.py`

- [ ] **Step 1: Write the failing tests**

```python
"""The policy receiver re-verifies the signature, so the forward must be
byte-for-byte the frame Anthropic sent, with its three webhook headers."""

from __future__ import annotations

import httpx
import pytest

from slashid_anthropic_forwarder.hook.checks import CheckFailed
from slashid_anthropic_forwarder.hook.policy import policy_check

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
    body = {"action": "deny", "deny_reason": "no", "reference_id": "ref"}
    async with client(lambda r: httpx.Response(200, json=body)) as c:
        verdict = await policy_check(c, url=URL, body=BODY, headers=HEADERS, timeout_s=1.0)
    assert verdict.action == "deny" and verdict.deny_reason == "no"
    assert verdict.reference_id == "ref"


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

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_policy.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.hook.checks'`.

- [ ] **Step 3: Implement** `hook/policy.py`:

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

A 200 deny is authoritative — the receiver turns its own internal errors into an explicit deny — so it is never softened by our fail mode.

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_policy.py -v && uv run ruff check .`. Expected: 7 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/hook anthropic/tests/test_policy.py
git commit -m "feat(anthropic): policy-receiver client forwarding the raw signed frame"
```

### Task 4.3: `preflight.py` — content check

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/hook/preflight.py`
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

from slashid_anthropic_forwarder.hook.checks import CheckFailed
from slashid_anthropic_forwarder.hook.preflight import preflight_check

ENDPOINT = "https://api.slashid.example"
IDENTITY = {"kind": "anthropic", "user_id": "user_01A"}
FILES = [
    AIAccessedFile(
        name="a.txt",
        content_hashes={"sha256": "aa", "sha1": "bb", "md5": "cc"},
        media_type="text/plain",
        byte_length=3,
        redacted_content="secret",
    ),
    AIAccessedFile(name="b.txt", content_hashes={"sha256": "dd"}),
]


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(overall: dict, **rest) -> httpx.Response:
    return httpx.Response(200, json={"overall": overall, **rest})


async def call(c: httpx.AsyncClient, **overrides):
    kwargs = dict(
        endpoint=ENDPOINT,
        push_token="tok",
        identity=IDENTITY,
        model=None,
        files=FILES,
        timeout_s=1.0,
    )
    kwargs.update(overrides)
    return await preflight_check(c, **kwargs)


async def test_request_shape_and_auth() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["json"] = json.loads(request.content)
        return ok({"allowed": True, "verified": True})

    async with client(handler) as c:
        await call(c, model="claude-opus-5")
    assert seen["url"] == f"{ENDPOINT}/ip/nhi/ai/preflight"
    assert seen["auth"] == "Bearer tok"
    body = seen["json"]
    assert body["identity_details"] == IDENTITY
    assert body["model"] == {"id": "claude-opus-5"}
    assert body["accessed_files"][0] == {
        "name": "a.txt",
        "content_hashes": {"sha256": "aa", "sha1": "bb", "md5": "cc"},
        "media_type": "text/plain",
        "byte_length": 3,
    }
    assert "redacted_content" not in body["accessed_files"][0]


async def test_model_omitted_when_null() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        return ok({"allowed": True, "verified": True})

    async with client(handler) as c:
        await call(c)
    assert "model" not in seen["json"]


async def test_verified_allow() -> None:
    async with client(lambda r: ok({"allowed": True, "verified": True})) as c:
        v = await call(c)
    assert v is not None and v.action == "allow" and v.source == "preflight"


async def test_verified_deny_carries_message() -> None:
    answer = {"allowed": False, "verified": True, "message": "a.txt is marked sensitive."}
    async with client(lambda r: ok(answer)) as c:
        v = await call(c)
    assert v is not None and v.action == "deny"
    assert v.deny_reason == "a.txt is marked sensitive."


async def test_unverified_returns_none() -> None:
    async with client(lambda r: ok({"allowed": True, "verified": False})) as c:
        assert await call(c) is None


async def test_over_cap_is_unverified_without_calling() -> None:
    called: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(1)
        return ok({"allowed": True, "verified": True})

    many = [AIAccessedFile(name=f"{i}.txt", content_hashes={"sha256": "x"}) for i in range(101)]
    async with client(handler) as c:
        assert await call(c, files=many) is None
    assert called == []


@pytest.mark.parametrize("status", [400, 401, 404, 503])
async def test_non_200_raises(status: int) -> None:
    async with client(lambda r: httpx.Response(status, text="x")) as c:
        with pytest.raises(CheckFailed):
            await call(c)
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_preflight.py -v`. Expected: collection ERROR, `ModuleNotFoundError`.

- [ ] **Step 3: Implement** `hook/preflight.py`:

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

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_preflight.py -v && uv run ruff check .`. Expected: 10 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/hook/preflight.py anthropic/tests/test_preflight.py
git commit -m "feat(anthropic): preflight client"
```

### Task 4.4: default `SLASHID_PREFLIGHT_ENABLED` to false

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/config.py`, `anthropic/tests/test_config.py`

The spec's configuration table says `SLASHID_PREFLIGHT_ENABLED` defaults to `false` — the endpoint has not shipped — but `config.py:29` currently defaults it to `True` and `tests/test_config.py:33` asserts that current value. Both flip here, before the composer starts reading the flag.

- [ ] **Step 1: Flip the assertion first** — in `tests/test_config.py`, `test_defaults_are_observe_only_and_fail_open`:

```python
    assert cfg.preflight_enabled is False
```

- [ ] **Step 2: Run to verify it fails** — `cd anthropic && uv run pytest tests/test_config.py -v`. Expected: `test_defaults_are_observe_only_and_fail_open` FAILS on `assert True is False`.

- [ ] **Step 3: Flip the default** — `config.py`, keeping the comment one line as it is:

```python
    # POST {endpoint}/ip/nhi/ai/preflight. Off until that endpoint ships.
    preflight_enabled: bool = False
```

- [ ] **Step 4: Run** — `cd anthropic && uv run pytest tests/test_config.py -v`. Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/config.py anthropic/tests/test_config.py
git commit -m "fix(anthropic): preflight defaults off until the endpoint ships"
```

### Task 4.5: `verdict.py` — concurrency, budget, fail mode, shadow mode

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/hook/verdict.py`
- Test: `anthropic/tests/test_verdict.py`, `anthropic/tests/test_decide.yaml`

Unknown top-level `type` is checked twice on purpose: `main.py` answers allow before it ever calls `decide`, and `decide` answers `bypass` for it as well, because `decide` is also called directly by these tests and must be safe on its own. Do not remove either.

- [ ] **Step 1: Write the failing tests**

```python
"""Composition: any deny denies; a check that cannot answer takes the fail
mode; config-test and unknown types bypass; shadow mode always allows but
keeps the composed verdict for the record."""

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

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.checks import Decision
from slashid_anthropic_forwarder.hook.frame import PromptFrame
from slashid_anthropic_forwarder.hook.verdict import decide, reference_id
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
    """``action``/``source`` are the answered verdict — what goes back to
    Anthropic. ``composed_*``, when given, is what the checks decided."""

    action: str
    source: str
    composed_action: str | None = None
    composed_source: str | None = None
    calls: list[str] | None = None  # sorted; None = don't care


def config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.example",
        "push_token": "t",
        "hook_signing_secret": SECRET,
        "shadow_mode": False,
        "preflight_enabled": True,
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


async def run(cfg: Config, handler, *, files: bool, body: bytes, fr: PromptFrame) -> Decision:
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
    decision = await run(
        config(**config_overrides), handler, files=files, body=body.encode(), fr=frame(frame_update)
    )
    answered = decision.answered
    assert (answered.action, answered.source) == (expected.action, expected.source)
    assert decision.blocked == answered.denied
    if expected.composed_action is not None:
        assert decision.composed.action == expected.composed_action
        assert decision.composed.source == expected.composed_source
    if answered.denied:
        assert answered.reference_id == reference_id("msg_1")
    if expected.calls is not None:
        assert sorted(calls) == expected.calls


async def test_deny_reason_is_capped_at_500_chars() -> None:
    handler, _ = router(
        Mock(body={"action": "deny", "deny_reason": "x" * 900}),
        Mock(body={"overall": {"allowed": True, "verified": True}}),
    )
    decision = await run(config(), handler, files=True, body=b"{}", fr=frame({}))
    answered = decision.answered
    assert answered.deny_reason.startswith("x" * 900)  # capped only on the wire
    assert answered.deny_reason.endswith("Start a new conversation to continue.")
    assert answered.to_wire()["deny_reason"] == "x" * 500


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
id: marker_denies_when_not_shadowed
policy: {body: {action: allow}}
preflight: {body: {overall: {allowed: true, verified: true}}}
config_overrides: {capture_deny_marker: SLASHID_DENY_ME}
frame_update: {}
files: true
body: '{"x": "SLASHID_DENY_ME"}'
expected: {action: deny, source: marker}
---
# Shadow mode is the default. Every check still runs; allow is answered, and
# the composed deny is what the record will say.
id: shadow_mode_runs_checks_but_allows
policy: {body: {action: deny, deny_reason: "no"}}
preflight: {body: {overall: {allowed: false, verified: true}}}
config_overrides: {shadow_mode: true}
frame_update: {}
files: true
body: "{}"
expected:
  action: allow
  source: shadow
  composed_action: deny
  composed_source: policy
  calls: [policy, preflight]
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_verdict.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.hook.verdict'`.

- [ ] **Step 3: Implement** `hook/verdict.py`:

```python
"""Compose the verdict: two optional checks, concurrently, under one budget.

Owns rule 2 of the design: what to answer when a check cannot. Any deny
denies; a failed or unverified check takes ``verdict_fail_mode``; a
disabled check is simply absent. Shadow mode evaluates and logs, then
answers allow, and the ``Decision`` keeps both verdicts so the record
stores the one that actually went out.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Mapping

import httpx
from slashid_ai_forwarder_core.events import AIAccessedFile

from ..config import Config
from .checks import ALLOW, CheckFailed, Decision, Verdict
from .frame import PromptFrame
from .policy import policy_check
from .preflight import preflight_check

log = logging.getLogger(__name__)

# Sticky denials: the offending content stays in the fresh round, so the
# conversation cannot recover. Anthropic's guidance is to say what to
# change; the only true answer is to start over.
RECOVERY = " Start a new conversation to continue."


def reference_id(webhook_id: str) -> str:
    """Same recipe as the Go policy receiver, so every record joins on one value."""
    return hashlib.sha256(webhook_id.encode()).hexdigest()[:32]


def _fail_mode(config: Config, why: str) -> Verdict:
    log.warning("verdict: %s; applying fail mode %s", why, config.verdict_fail_mode)
    if config.verdict_fail_mode == "deny":
        return Verdict(
            "deny",
            deny_reason="Your organization's policy check is unavailable.",
            source="fail_mode",
        )
    return Verdict("allow", source="fail_mode")


async def _settle(name: str, task: Awaitable[Verdict | None], config: Config) -> Verdict:
    try:
        result = await task
    except CheckFailed as exc:
        return _fail_mode(config, f"{name} failed: {exc}")
    if result is None:
        return _fail_mode(config, f"{name} unverified")
    return result


def _answer(config: Config, composed: Verdict) -> Decision:
    if config.shadow_mode and composed.denied:
        return Decision(composed=composed, answered=Verdict("allow", source="shadow"))
    return Decision(composed=composed, answered=composed)


async def decide(
    frame: PromptFrame,
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    files: list[AIAccessedFile],
    config: Config,
    client: httpx.AsyncClient,
) -> Decision:
    lower = {k.lower(): v for k, v in headers.items()}
    webhook_id = lower.get("webhook-id", frame.request_id)
    ref = reference_id(webhook_id)

    if frame.type != "prompt" or frame.is_connection_test():
        # The policy receiver denies both; the protocol wants allow for both.
        return _answer(config, Verdict("allow", source="bypass"))

    budget_s = config.verdict_budget_ms / 1000
    checks: list[tuple[str, Awaitable[Verdict | None]]] = []
    if config.policy_url:
        checks.append(
            (
                "policy",
                policy_check(
                    client,
                    url=config.policy_url,
                    body=raw_body,
                    headers=headers,
                    timeout_s=budget_s,
                ),
            )
        )
    if config.preflight_enabled and files and frame.actor.id:
        # A null actor id cannot be resolved server-side; chunk 3 drops such
        # events, and a 400 here would only engage the fail mode.
        checks.append(
            (
                "preflight",
                preflight_check(
                    client,
                    endpoint=config.endpoint,
                    push_token=config.push_token,
                    identity={"kind": "anthropic", "user_id": frame.actor.id},
                    model=frame.model,
                    files=files,
                    timeout_s=budget_s,
                ),
            )
        )

    results: list[Verdict] = []
    if config.capture_deny_marker and config.capture_deny_marker.encode() in raw_body:
        results.append(
            Verdict(
                "deny",
                deny_reason="Denied by the SlashID capture test marker.",
                source="marker",
            )
        )
    if checks:
        try:
            settled = await asyncio.wait_for(
                asyncio.gather(*(_settle(n, t, config) for n, t in checks)),
                timeout=budget_s,
            )
            results.extend(settled)
        except TimeoutError:
            results.append(_fail_mode(config, "verdict budget exceeded"))

    composed = next((r for r in results if r.denied), ALLOW)
    if composed.denied:
        reason = (composed.deny_reason or "Blocked by your organization's policy.") + RECOVERY
        composed = Verdict("deny", deny_reason=reason, reference_id=ref, source=composed.source)
    decision = _answer(config, composed)
    log.info(
        "verdict %s: composed %s via %s, answered %s (shadow_mode=%s, checks=%s)",
        webhook_id,
        composed.action,
        composed.source,
        decision.answered.action,
        config.shadow_mode,
        [n for n, _ in checks],
    )
    return decision
```

On budget timeout `wait_for` cancels the gathered tasks, and the cancellation propagates into the in-flight httpx call, so the `budget_exceeded_takes_fail_mode` case completes in about the budget, not the handler's 2 s sleep. No extra cancellation handling is needed.

`Decision.blocked` is what chunk 5 stamps `guardrail_intervened` from — `AIStopReason` already carries that value (`shared/src/slashid_ai_forwarder_core/events.py:48`, used by `stop_reason` at line 294), so no schema change. A shadow-mode deny is `blocked == False`: the request ran, a successor frame arrives, and the record completes as the allowed invocation it turned out to be.

- [ ] **Step 4: Run** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_verdict.py -v && uv run ruff check . && uv run ty check`. Expected: 18 passed (16 yaml cases + 2), no warnings.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/hook/verdict.py anthropic/tests/test_verdict.py anthropic/tests/test_decide.yaml
git commit -m "feat(anthropic): verdict composition with fail mode and shadow mode"
```

---

---

## Chunk 5: Addresses, the record, and the pending store

Three modules at the package root — above `hook/` and `compliance/`, because both packages call all three. `address.py` answers "what is this invocation called?", and the design's measurement is the whole reason it has three answers rather than one: a digest over the transcript prefix produced **200 keys on the frame side, 302 on the reader side and zero in common**, so the only key both sources can compute is a model-minted `tool_use.id`. A run without one gets a hook-local address that no reader may ever emit under, and every frame additionally files a `tail:` record keyed on a digest of its whole transcript — hook-local too, and therefore allowed to use the encoding the cross-source key cannot. A fourth space, `deny:`, exists for the same reason the third does and belongs to Reader A: it arrives with that reader in Chunk 6, and this chunk builds the three the hook needs. `record.py` holds the partial event as a serialized mapping plus the control envelope around it, and owns the 1 MiB bound. `store.py` is the port: six operations, a Firestore adapter behind them, and `claim` — the compare-and-set that decides which of a completing writer and the deadline sweep gets to push. Nothing here reads `Config` and nothing here pushes; wiring the store into the app and the tick is the next chunk's job.

**On the emulator.** The design says "against a fake and, when credentials allow, the Firestore emulator". Checked: `gcloud beta emulators firestore` exists as a command but `gcloud components list` reports **Cloud Firestore Emulator as `Not Installed`**, the per-subproject CI command is a bare `uv run pytest` with no emulator bootstrap, and the one sibling that already faced this choice — `vertex/tests/test_firestore_checkpoint.py:3-5` — records it explicitly ("Firestore emulator is available but overkill"). So: **a fake client**, in the same shape as vertex's, extended with what this store actually uses — `create`, `set(merge=True)` deep merge, `ArrayUnion`/`ArrayRemove` transforms, `update` under a `last_update_time` precondition, and a query with two filters, an order and a limit. Two things a fake cannot prove and that no test here should pretend to: that the real backend rejects a stale `last_update_time` (it raises `FailedPrecondition`; the fake mimics it), and that the composite index `due` needs exists. Both belong to the live run in the deploy chunk, and the store's docstring names the index so the Terraform has something to copy.

### Task 5.1: `google-cloud-firestore` in `anthropic/pyproject.toml`

**Files:**
- Modify: `anthropic/pyproject.toml` (the `dependencies` list), `uv.lock`

- [ ] **Step 1: Add the dependency** — same lower bound as `vertex/pyproject.toml:12`, which resolves to 2.30.0 today:

```bash
cd anthropic && uv add "google-cloud-firestore>=2.16"
```

- [ ] **Step 2: Verify it imports and the async surface is the one this chunk uses**

```bash
cd anthropic && uv run python -c "
from google.cloud.firestore_v1 import AsyncClient
from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.transforms import ArrayRemove, ArrayUnion
print(FieldFilter('a', '<=', 1).op_string, type(AsyncClient.write_option(last_update_time='t')).__name__)
"
```

Expected: `<= LastUpdateOption`. Two facts that matter later and are cheap to confirm now: `FieldFilter` exposes `field_path`/`op_string`/`value` (the fake pattern-matches on those), and `write_option` is a **static** method on the client, so `client.write_option(...)` works on the fake too.

- [ ] **Step 3: Commit**

```bash
git add anthropic/pyproject.toml uv.lock
git commit -m "chore(anthropic): google-cloud-firestore dependency"
```

### Task 5.2: `address.py` — the joinable address and the hook address

`joinable_address` is the only key both sources compute, and it is deliberately dumb: the first `tool_use.id` in a completed assistant run, in block order, across however many messages that run arrived as. No ordinal, because a hundred-plus sub-conversations share a `session_id` and any per-session counter collides. No prefix, because a `toolu_` id already carries one. `None` when the run has no tool call at all — **194 of 284** measured trailing runs had one, but only **6 of 14** on claude.ai, so the `None` branch is the common case on one surface and the rare case on the other.

`hook_address` covers that `None`: `hook:` plus the delivery id, which is unique per delivery and needs no agreement with anybody. It exists to be *unjoinable on purpose* — Reader B must never emit under it, because the hook already reported that invocation and first-completed-wins would count it twice rather than merge it.

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/address.py`
- Test: `anthropic/tests/test_address.py`, `anthropic/tests/test_joinable_address.yaml`

- [ ] **Step 1: Write the failing tests** — `tests/test_address.py`:

```python
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
```

with `tests/test_joinable_address.yaml`:

```yaml
# No assistant run at all — the frame is a first turn.
id: first_turn_has_no_run_to_address
fixture: frame_first_turn
expected: null
---
id: an_attachment_only_frame_has_no_run_either
fixture: frame_attachment
expected: null
---
# The run answered in text. Joinable is about tool calls, not about content:
# this one is the hook's alone, under a hook: address.
id: a_text_only_run_is_unjoinable
fixture: frame_after_shadow_deny
expected: null
---
id: the_single_tool_call_is_the_address
fixture: frame_tool_result
expected: toolu_01Dqhr2d1w2UCUqbXhCSGutC
---
# Two tool_use blocks in one message: block order decides, and it is stable
# across both surfaces because both carry the run's blocks in order.
id: two_parallel_calls_take_the_first
fixture: frame_multi_tool
expected: toolu_01DqPgvQiPYuWCXJfKFEhojJ
---
id: a_failed_tool_call_still_addresses_the_run
fixture: frame_tool_error
expected: toolu_01GSt4gp6UqW6a5RsxRvyn8d
---
id: an_mcp_tool_call_addresses_like_any_other
fixture: frame_mcp_tool
expected: toolu_01YPqDRNnctTJwvCvzBR7GEs
---
# Two sessions' worth of frames share this session_id; the addresses do not
# collide, because neither is a function of the session.
id: the_subagent_parents_task_call
fixture: frame_subagent_parent
expected: toolu_01WjE9YXynBLJThi28oCWQLr
---
id: the_subagent_childs_own_call
fixture: frame_subagent_child
expected: toolu_01UbdhcQRwkxR8JFoAGZi2i9
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_address.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.address'`.

- [ ] **Step 3: Implement** `src/slashid_anthropic_forwarder/address.py`:

```python
"""Addresses — the keys a pending record can be filed under.

Only a model-minted ``tool_use.id`` agrees across the two sources, and
that was measured rather than reasoned: over one session present in both
the captured frames and the stored transcript, a digest over the
transcript prefix through each assistant run produced 200 keys on the
frame side, 302 on the reader side and **zero in common** — still zero
after dropping synthetic markers, still zero with every text block
removed. The stored transcript is a different projection of the
conversation (a prepended synthetic marker, turns from before capture
began, sub-agent turns the frame never shows), so no function of the
message sequence can survive the crossing. An opaque token the model
minted once can, and does.

So there are four address spaces, and which one a record gets decides
who may write it. Three of them are here; the fourth belongs to Reader A
and lands with it:

- ``joinable_address`` — both sources compute it; either may open the
  record and the other may complete it.
- ``hook_address`` — the hook alone, for a run with no tool call. No
  reader can compute a delivery id, which is the point: emitting the
  same invocation under a second key would double-count it, since the
  terminal's dedup is first-completed-wins and never merges.
- ``tail_address`` — the hook alone, for the fresh round a frame carries
  that no successor may ever report. Hook-local, so it may use an
  encoding the cross-source key cannot.
- ``deny_address`` — **not here**: Reader A's, added in the chunk that
  builds it. It is a fourth space rather than a second use of ``hook:``
  because one frame can carry an unjoinable previous run *and* an
  honoured deny on its fresh round, and one key for both would merge two
  unrelated invocations into one record.
"""

from __future__ import annotations

from collections.abc import Sequence

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicRequestMessage,
    AnthropicToolUseBlock,
)


def joinable_address(run: Sequence[AnthropicRequestMessage]) -> str | None:
    """The first ``tool_use.id`` in a completed assistant run, or ``None``.

    ``run`` is one response, which may have arrived as several consecutive
    assistant messages; block order across them decides. No ordinal enters
    the key: one ``session_id`` carries a hundred-plus sub-conversations,
    so any per-session counter collides.

    ``None`` means unjoinable — 194 of 284 measured trailing runs had a
    tool call, but only 6 of 14 on claude.ai.
    """
    for message in run:
        for block in message.content:
            if isinstance(block, AnthropicToolUseBlock):
                return block.id
    return None


def hook_address(webhook_id: str) -> str:
    """The address of a run the hook alone can see: ``hook:`` + the delivery id.

    Unique without agreeing with anything, because nothing else has to
    compute it. A reader that finds an unjoinable run leaves it alone.
    """
    return f"hook:{webhook_id}"
```

Import exactly this much and no more. Task 5.3 brings `hashlib`, `unicodedata` and three further block types in *with* the code that uses them, so the `--fix` in the next step has nothing to strip — an unused import written one task early is deleted silently, and `ruff check --fix` exits 0 while doing it.

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_address.py -v && uv run ty check`. Expected: `13 passed` (9 yaml cases, 4 plain), ruff and ty clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/address.py \
        anthropic/tests/test_address.py anthropic/tests/test_joinable_address.yaml
git commit -m "feat(anthropic): joinable and hook addresses for a pending record"
```

### Task 5.3: `address.py` — `tail_address` and its canonical encoding

Emit-previous leaves a session's final round with no successor to report it, and `accessed_files` belong to the round the model *consumed*, so that round's `Read` would vanish. Every frame therefore also writes a tail record, input-only, addressed by a digest over its **whole** transcript.

The encoding is the contract, not an implementation detail, because two call sites in the same codebase have to agree byte-for-byte: the frame that writes the tail, and the **successor** frame that discards it. Frame N+1 reconstructs frame N's transcript by dropping its own trailing assistant run and the round after it — which is exactly `split_transcript(...).before` — recomputes the key and retires it `superseded`. Get the encoding wrong in a way that is merely *stable* and nothing fails loudly: every tail record survives to its deadline and is pushed, duplicating a round the successor already reported properly.

Hence the spelling below, and a test that asserts the bytes rather than only the digest:

- Every field is emitted as a 4-byte big-endian length followed by its UTF-8 bytes, so no separator can be forged by content.
- Text is NFC-normalized **and then** truncated to `TEXT_PREFIX_CHARS` codepoints. That order matters: truncating first can split a combining sequence and change what normalization produces.
- Four block types contribute — `text` (its prefix), `tool_use` (id, name), `tool_result` (tool_use_id, the error flag) and `attachment` (file name, text prefix). `thinking` and every unknown type contribute **nothing**, deliberately: frames carry no thinking blocks today, and a block type invented next quarter must not move an existing key.
- Each message contributes its role and the number of blocks that actually contributed, so a skipped block cannot be confused with an absent one.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/address.py` (append)
- Test: `anthropic/tests/test_address.py` (append), `anthropic/tests/test_tail_reconstruction.yaml`

- [ ] **Step 1: Write the failing tests** — extend the import to `from slashid_anthropic_forwarder.address import _canonical_bytes, hook_address, joinable_address, tail_address` and append:

```python
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
```

adding `import unicodedata` to the import block. `tests/test_tail_reconstruction.yaml`:

```yaml
# [user, assistant, user] — the predecessor was the opening prompt alone.
id: the_opening_prompt_is_the_first_tail
fixture: frame_tool_result
predecessor_messages: 1
---
# [user, assistant, user, assistant, user] — the predecessor ended on the
# round that fed the run this frame reports.
id: a_mid_session_round
fixture: frame_subagent_child
predecessor_messages: 3
---
# Deferred-tool loading: the predecessor's consumed round spanned two user
# messages, so `before` has to keep both or the key moves.
id: a_predecessor_whose_round_spanned_two_user_messages
fixture: frame_mcp_tool
predecessor_messages: 4
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_address.py -v`. Expected: collection ERROR, `ImportError: cannot import name '_canonical_bytes' from 'slashid_anthropic_forwarder.address'`.

- [ ] **Step 3: Implement** — first widen the module's imports to what the tail digest needs, replacing the block Task 5.2 wrote:

```python
import hashlib
import unicodedata
from collections.abc import Sequence

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicRequestMessage,
    AnthropicTextBlock,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
)

# How much of a text block contributes to a tail digest. A Claude Code
# transcript reaches 1.86 MB and a frame arrives every round, so the digest
# has to be cheap; a 256-character prefix per block, together with the block
# count, the roles and every tool id, was exact over the measured corpus —
# 239 distinct keys from 284 deliveries, zero false merges, zero false
# splits against the toolu_ ids as ground truth.
TEXT_PREFIX_CHARS = 256

_TAIL_VERSION = b"tail/1"
```

then append:

```python
def tail_address(transcript: Sequence[AnthropicRequestMessage], session_id: str | None) -> str:
    """``tail:`` + a digest over a frame's WHOLE transcript.

    Hook-local: no reader ever computes it, so it may use an encoding that
    the cross-source key provably cannot. Intra-source it was exact — 239
    distinct keys from 284 deliveries, zero false merges and zero false
    splits against the toolu_ ids as ground truth.

    It must be **reconstructible**, because the successor frame's whole job
    is to discard the record: frame N+1 drops its own trailing assistant run
    and the round after it — ``split_transcript(...).before`` — and calls
    this with the result. The canonical encoding is therefore part of the
    contract, spelled out in ``_canonical_bytes``.
    """
    return "tail:" + hashlib.sha256(_canonical_bytes(transcript, session_id)).hexdigest()


def _canonical_bytes(
    transcript: Sequence[AnthropicRequestMessage], session_id: str | None
) -> bytes:
    """The byte stream a tail digest is taken over.

    Every field is length-prefixed (4-byte big-endian) so content cannot
    forge a separator. Per message: the role, then the number of blocks that
    contributed, then each contributing block's fields. ``thinking`` and
    unknown block types contribute nothing — frames carry no thinking blocks
    today, and a block type invented next quarter must not move an existing
    key. The version tag leads, so a future encoding change is a new key
    space rather than a silent re-addressing of live records.
    """
    out = [
        _field(_TAIL_VERSION),
        _field(_prefix(session_id)),
        _field(str(len(transcript)).encode()),
    ]
    for msg in transcript:
        emitted = [f for f in (_block_fields(b) for b in msg.content) if f is not None]
        out.append(_field(msg.role.encode("utf-8")))
        out.append(_field(str(len(emitted)).encode()))
        for fields in emitted:
            out.extend(_field(f) for f in fields)
    return b"".join(out)


def _field(raw: bytes) -> bytes:
    return len(raw).to_bytes(4, "big") + raw


def _prefix(text: str | None) -> bytes:
    """NFC-normalize, THEN truncate. The other order can split a combining
    sequence and change what normalization produces."""
    if not text:
        return b""
    return unicodedata.normalize("NFC", text)[:TEXT_PREFIX_CHARS].encode("utf-8")


def _block_fields(block: object) -> list[bytes] | None:
    """The fields one content block contributes, or ``None`` to skip it."""
    if isinstance(block, AnthropicTextBlock):
        return [b"t", _prefix(block.text)]
    if isinstance(block, AnthropicToolUseBlock):
        return [b"u", block.id.encode("utf-8"), _prefix(block.name)]
    if isinstance(block, AnthropicToolResultBlock):
        # The result's content is not hashed: it is capped at 10 KB on the
        # reader's side and untruncated here, and the pairing id already
        # identifies it uniquely.
        return [b"r", block.tool_use_id.encode("utf-8"), b"1" if block.is_error else b"0"]
    if isinstance(block, AnthropicAttachmentBlock):
        return [b"a", _prefix(block.file_name), _prefix(block.text)]
    return None
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_address.py -v && uv run ty check`. Expected: `22 passed` (13 from Task 5.2, 6 plain and 3 yaml cases here). If the byte-literal test fails, read the diff before touching the literal: the literal is the golden value, and the four-byte lengths make the offending field obvious.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/address.py \
        anthropic/tests/test_address.py anthropic/tests/test_tail_reconstruction.yaml
git commit -m "feat(anthropic): reconstructible tail address over a frame's transcript"
```

### Task 5.4: `record.py` — the partial event, the control envelope, the 1 MiB bound

The stored document is the event object itself, as a **serialized mapping** rather than a validated model: `AIInvocationObservedV1` requires `parsed_as`, which depends on which sources end up contributing and is unknowable while the record is pending, and `_WireModel` sets `extra="forbid"` (`shared/src/slashid_ai_forwarder_core/events.py:61`), so the control envelope cannot ride inside it either. Validation happens at push, in `to_event`, on the one path that can log and retry — and that is where `parsed_as` is finally decided, from `contributed`: `anthropic-joined` only when more than one source actually supplied a field.

The 1 MiB bound lives here rather than in the adapter. The adapter should write what it is handed; deciding *what to drop* is a statement about the event's content. Raw text goes first and in size order (the input is a whole transcript, up to 1.86 MB; the output is one run), then file contents, then the file and tool lists outright — so the function always returns something writable. Every elision sets a marker in place of the text and an `elided` flag on the record, because an event with `content_hashes` and `byte_length` but no `redacted_text` is otherwise indistinguishable from an invocation that carried no text at all.

One envelope subtlety that shapes the API: `webhook_ids` and `contributed` are append-only sets (43 of 239 measured invocations were revealed by more than one delivery), and a plain merge would have the second writer clobber the first. So the field builders return an `Append` marker, which the adapter translates into the backend's array transform. That keeps Firestore's sentinels out of the record module and out of the protocol.

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/record.py`
- Test: `anthropic/tests/test_record.py`, `anthropic/tests/test_elision.yaml`

- [ ] **Step 1: Write the failing tests** — `tests/test_record.py`:

```python
"""The pending record: the partial event, the envelope, and the 1 MiB bound."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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
    PARSED_AS_HOOK,
    Append,
    PendingRecord,
    event_fields,
    from_document,
    open_fields,
    parsed_as,
    to_event,
)

NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)


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
        an_event(), webhook_id="msg_011CfFXrZo19wubUcJjnSJa9", verdict="allow",
        composed_verdict="deny", contributed=HOOK,
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
```

with one helper beside the imports, used by both tests:

```python
def json_size(body: dict[str, object]) -> int:
    return len(json.dumps(body, separators=(",", ":")).encode())
```

with `tests/test_elision.yaml`:

```yaml
# The common case by a mile: 475 of 492 measured frames fit with room to
# spare, and nothing is copied or rewritten on this path.
id: a_small_event_is_untouched
input_chars: 5000
output_chars: 500
file_chars: 500
elided: false
surviving: [input, output, file]
---
# The largest measured transcript was 1.86 MB. Its input goes and nothing
# else has to.
id: a_large_transcript_loses_its_input_only
input_chars: 1500000
output_chars: 500
file_chars: 500
elided: true
surviving: [output, file]
---
# The steps run in size order and stop at the first fit, so the output is
# only reached when the input alone was not enough — which needs an output
# that still breaks the bound on its own.
id: a_large_answer_too_and_the_output_follows
input_chars: 1200000
output_chars: 1200000
file_chars: 500
elided: true
surviving: [file]
---
# Three oversized fields: everything raw goes and the event is still a
# writable record of the invocation, hashes intact.
id: everything_raw_goes_before_the_write_fails
input_chars: 1200000
output_chars: 1200000
file_chars: 1200000
elided: true
surviving: []
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_record.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.record'`.

- [ ] **Step 3: Implement** `src/slashid_anthropic_forwarder/record.py`:

```python
"""The pending record: a partial event plus the control envelope around it.

The event is stored as a serialized mapping, not a validated model.
``AIInvocationObservedV1`` requires ``parsed_as``, which depends on which
sources end up contributing and is unknowable while the record is
pending, and its base sets ``extra="forbid"``, so the envelope cannot
ride inside it. Validation happens at push, in ``to_event`` — the one
path that can log and retry.

The 1 MiB document bound is enforced here rather than in the storage
adapter: the adapter should write what it is handed, and deciding what
to drop is a statement about the event's content. Dropping raw text sets
a marker so the absence reads as elision rather than as an invocation
that carried none.

There is no ``to_document``: nothing ever writes a whole record. A
writer supplies fields, the store merges them, and ``from_document``
reads the result back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from slashid_ai_forwarder_core.events import AIInvocationObservedV1

# Firestore's own document limit is 1 MiB including field names and
# indexing overhead; the event is bounded below it and the envelope is a
# few hundred bytes.
MAX_EVENT_BYTES = 1024 * 1024
ELISION = "[slashid: elided — the pending record exceeded 1 MiB]"

# `contributed` values: which source actually supplied a field, which is
# not the same as which visited.
HOOK = "hook"
COMPLIANCE = "compliance"

# The only member the expectation set can ever have. A frame-built record
# is complete on arrival except for attachment byte digests, so the common
# case waits for nothing.
FILE_DIGESTS = "file_digests"

PARSED_AS_HOOK = "anthropic-inference-hook"
PARSED_AS_COMPLIANCE = "anthropic-compliance"
PARSED_AS_JOINED = "anthropic-joined"


@dataclass(frozen=True)
class Append:
    """Append-to-set marker for a field two writers may both extend.

    Backend-neutral on purpose: the adapter translates it into Firestore's
    array transform, so no sentinel from a vendor SDK reaches this module
    or the ``PendingStore`` protocol.
    """

    values: tuple[str, ...]


@dataclass(frozen=True)
class PendingRecord:
    """One pending invocation, as the store holds it right now.

    ``event`` is the serialized partial ``AIInvocationObservedV1``. The
    rest is the control envelope, which never reaches the wire.
    """

    address: str
    event: dict[str, Any]
    deadline: datetime
    # When the sweep may look at this record again: the deadline on
    # creation, the end of the lease while claimed, the end of the backoff
    # after a failed push. One field, so "claim absent or expired" is a
    # single inequality rather than a second one the index has to carry.
    next_attempt_at: datetime
    webhook_ids: list[str] = field(default_factory=list)
    verdict: str | None = None
    composed_verdict: str | None = None
    awaiting: list[str] = field(default_factory=list)
    contributed: list[str] = field(default_factory=list)
    attempts: int = 0
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    tombstoned_at: datetime | None = None
    elided: bool = False

    @property
    def ready(self) -> bool:
        """Readiness is a state, not a transition. A record born ready — under
        emit-previous, most of them — has no transition at all, so asking
        "did this call empty the expectation set?" cannot arbitrate."""
        return self.tombstoned_at is None and not self.awaiting


def event_fields(event: AIInvocationObservedV1) -> dict[str, Any]:
    """Serialize a partial event into merge fields, bounded at 1 MiB."""
    body, elided = _bound(event.model_dump(mode="json", exclude_none=True))
    fields: dict[str, Any] = {"event": body}
    if elided:
        # Only ever set, never cleared: a later merge of a small field must
        # not make an elided record look intact.
        fields["elided"] = True
    return fields


def open_fields(
    event: AIInvocationObservedV1,
    *,
    webhook_id: str,
    contributed: str,
    verdict: str | None = None,
    composed_verdict: str | None = None,
) -> dict[str, Any]:
    """The fields that open a record — or merge into one already open.

    ``webhook_ids`` appends rather than replaces: 43 of 239 measured
    invocations were revealed by more than one delivery, and Reader A
    matches a denial against any of them. A ``None`` verdict is omitted
    rather than merged, so a reader-opened record cannot erase the verdict
    a frame supplied.
    """
    fields = event_fields(event)
    fields["webhook_ids"] = Append((webhook_id,))
    fields["contributed"] = Append((contributed,))
    if verdict is not None:
        fields["verdict"] = verdict
    if composed_verdict is not None:
        fields["composed_verdict"] = composed_verdict
    return fields


def parsed_as(contributed: list[str]) -> str:
    """``anthropic-joined`` only when more than one source contributed."""
    sources = set(contributed)
    if len(sources) > 1:
        return PARSED_AS_JOINED
    if sources == {COMPLIANCE}:
        return PARSED_AS_COMPLIANCE
    return PARSED_AS_HOOK


def to_event(record: PendingRecord) -> AIInvocationObservedV1:
    """Validate a record into the event that goes on the wire.

    Raises ``ValidationError`` (a ``ValueError``) on a record that never
    became a whole event — which is why this runs at push, where the
    caller can log it and retry, and not on the request path.
    """
    return AIInvocationObservedV1.model_validate(
        {**record.event, "parsed_as": parsed_as(record.contributed)}
    )


def from_document(address: str, data: dict[str, Any]) -> PendingRecord:
    return PendingRecord(
        address=address,
        event=data.get("event") or {},
        deadline=data["deadline"],
        next_attempt_at=data["next_attempt_at"],
        webhook_ids=list(data.get("webhook_ids") or []),
        verdict=data.get("verdict"),
        composed_verdict=data.get("composed_verdict"),
        awaiting=list(data.get("awaiting") or []),
        contributed=list(data.get("contributed") or []),
        attempts=int(data.get("attempts") or 0),
        claim_owner=data.get("claim_owner"),
        claim_expires_at=data.get("claim_expires_at"),
        tombstoned_at=data.get("tombstoned_at"),
        elided=bool(data.get("elided")),
    )


def _sizeof(body: dict[str, Any]) -> int:
    return len(json.dumps(body, separators=(",", ":")).encode())


def _bound(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Drop raw text until the event fits, largest field first.

    The size check runs before any copying, so the common case — 475 of 492
    measured frames — pays one serialization and nothing else. Never fails:
    the last step drops the file and tool lists outright, leaving identity,
    hashes and the envelope, which cannot approach the bound.
    """
    if _sizeof(body) <= MAX_EVENT_BYTES:
        return body, False
    body = json.loads(json.dumps(body))  # detach: the caller's event stays whole
    elided = False
    for step in (_elide_input, _elide_output, _elide_files, _drop_lists):
        elided = step(body) or elided
        if _sizeof(body) <= MAX_EVENT_BYTES:
            break
    return body, elided


def _elide_input(body: dict[str, Any]) -> bool:
    # The input is the whole transcript — 1.86 MB at the measured maximum —
    # so it goes first.
    return _elide_text(body.get("input"), "redacted_text")


def _elide_output(body: dict[str, Any]) -> bool:
    return _elide_text(body.get("output"), "redacted_text")


def _elide_files(body: dict[str, Any]) -> bool:
    return any(
        [_elide_text(entry, "redacted_content") for entry in body.get("accessed_files") or []]
    )


def _drop_lists(body: dict[str, Any]) -> bool:
    body.pop("accessed_files", None)
    body.pop("used_tools", None)
    return True


def _elide_text(holder: Any, key: str) -> bool:
    if isinstance(holder, dict) and holder.get(key) is not None:
        holder[key] = ELISION
        return True
    return False
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_record.py -v && uv run ty check`. Expected: `13 passed` (9 plain, 4 yaml cases). The `PARSED_AS_HOOK == PARSED_AS` assertion is the guard against the hook envelope's own constant drifting from this one; if it fails, the two strings are out of step, not the test.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/record.py \
        anthropic/tests/test_record.py anthropic/tests/test_elision.yaml
git commit -m "feat(anthropic): pending record, control envelope and the 1 MiB bound"
```

### Task 5.5: `store.py` — the protocol, the fake, and the write path

Six operations, because that is what the design's table defines and what another cloud would have to reimplement. Everything backend-specific stays below the protocol: the document id, the array transforms, the TTL policy keyed off `tombstoned_at`, and the composite index behind `due`.

Three details of the write path are load-bearing and each has a test:

- **`upsert` creates or merges, and merging never moves the deadline.** A second delivery revealing the same invocation must not extend the wait. Implemented as `create` first, falling back to a merge on `AlreadyExists`, which is atomic without a transaction.
- **`complete` never creates.** A record that does not exist was never opened by a frame, and inventing one here would resurrect an invocation that was already pushed and tombstoned.
- **Both are a no-op on a tombstoned address and say so in the outcome.** That is the whole mechanism suppressing a late reader's duplicate, and `SLASHID_TOMBSTONE_TTL_SECONDS` (7200) is how long it holds.

One Firestore behaviour the fake must mirror exactly, because getting it wrong makes `due` silently return nothing: `FieldFilter("tombstoned_at", "==", None)` is normalized into an `IS_NULL` filter, and a document that **lacks** the field does not match it. Hence `upsert` writes `tombstoned_at: None` explicitly on creation.

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/store.py`
- Create: `anthropic/tests/fake_firestore.py`
- Test: `anthropic/tests/test_store.py`

- [ ] **Step 1: Write the fake** — `tests/fake_firestore.py`. It is test infrastructure, not a test, and it lives in its own module so Chunk 6's `test_pending.py` can import it the same way (`from tests.fake_firestore import FakeFirestore` — `anthropic/tests/__init__.py` already makes `tests` a package and pytest's default import mode puts `anthropic/` on the path):

```python
"""In-memory stand-in for ``firestore.AsyncClient``.

Covers exactly what ``FirestorePendingStore`` uses: ``create`` (raising
``AlreadyExists``), ``set(merge=True)`` with a deep merge and array
transforms, ``update`` under a ``last_update_time`` precondition, and a
query with filters, an order and a limit. The emulator would cover more,
but it is not installed in this environment or in CI, and the sibling
store made the same call — see ``vertex/tests/test_firestore_checkpoint.py``.

Two real behaviours are mimicked deliberately, because a fake that got
them wrong would hide a bug rather than surface one:

- ``FieldFilter(f, "==", None)`` normalizes to an ``IS_NULL`` operator,
  and a document without the field does NOT match it.
- ``update`` raises ``NotFound`` on a missing document and
  ``FailedPrecondition`` when the ``last_update_time`` no longer matches.

``on_get`` is the one thing here the real client has no analogue for: a
callback fired after a snapshot is taken, so a single-threaded test can
slip a competing writer in between a read and the compare-and-set that
follows it. Without it the CAS is unreachable from a sequential test —
the lease guard answers first — and the precondition above would be
mimicked but never exercised.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore_v1 import AsyncClient
from google.cloud.firestore_v1.transforms import ArrayRemove, ArrayUnion


class FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None, update_time: int | None) -> None:
        self.id = doc_id
        self.exists = data is not None
        self.update_time = update_time
        self._data = data

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, client: FakeFirestore, path: str, doc_id: str) -> None:
        self._client, self._path, self.id = client, path, doc_id

    async def get(self) -> FakeSnapshot:
        held = self._client.docs.get(self._path)
        snapshot = (
            FakeSnapshot(self.id, None, None)
            if held is None
            else FakeSnapshot(self.id, held[0], held[1])
        )
        if self._client.on_get is not None:
            # Taken already, so a writer that runs now leaves this caller
            # holding a stale one — which is the race `claim` has to lose.
            await self._client.on_get(self._path)
        return snapshot

    async def create(self, data: dict[str, Any]) -> None:
        if self._path in self._client.docs:
            raise AlreadyExists(self._path)
        self._client.write(self._path, copy.deepcopy(data))

    async def set(self, data: dict[str, Any], merge: bool = False) -> None:
        held = self._client.docs.get(self._path)
        base = copy.deepcopy(held[0]) if (merge and held is not None) else {}
        self._client.write(self._path, _merge(base, copy.deepcopy(data)))

    async def update(self, data: dict[str, Any], option: Any = None) -> None:
        held = self._client.docs.get(self._path)
        if held is None:
            raise NotFound(self._path)
        if option is not None and getattr(option, "_last_update_time", None) != held[1]:
            raise FailedPrecondition(f"stale precondition on {self._path}")
        self._client.write(self._path, _merge(copy.deepcopy(held[0]), copy.deepcopy(data)))


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, ArrayRemove):
            base[key] = [x for x in base.get(key) or [] if x not in value.values]
        elif isinstance(value, ArrayUnion):
            held = list(base.get(key) or [])
            base[key] = held + [x for x in value.values if x not in held]
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


class FakeQuery:
    def __init__(
        self,
        collection: FakeCollectionReference,
        predicates: list[tuple[str, Any, Any]] | None = None,
        order: str | None = None,
        bound: int | None = None,
    ) -> None:
        self._collection = collection
        self._predicates = predicates or []
        self._order, self._bound = order, bound

    def where(self, *, filter: Any) -> FakeQuery:
        # The real client's keyword is `filter`; shadowing the builtin here
        # is what makes the call sites identical.
        predicate = (filter.field_path, filter.op_string, filter.value)
        return FakeQuery(self._collection, [*self._predicates, predicate], self._order, self._bound)

    def order_by(self, field_path: str) -> FakeQuery:
        return FakeQuery(self._collection, self._predicates, field_path, self._bound)

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(self._collection, self._predicates, self._order, count)

    async def stream(self):  # -> AsyncIterator[FakeSnapshot]
        prefix = self._collection.prefix
        rows = [
            (path.removeprefix(prefix), held[0], held[1])
            for path, held in self._collection.client.docs.items()
            if path.startswith(prefix)
        ]
        for field_path, op, value in self._predicates:
            rows = [row for row in rows if _matches(row[1].get(field_path), op, value)]
        if self._order:
            rows.sort(key=lambda row: row[1][self._order])
        for row in rows[: self._bound] if self._bound else rows:
            yield FakeSnapshot(row[0], row[1], row[2])


def _matches(actual: Any, op: Any, value: Any) -> bool:
    name = getattr(op, "name", op)
    if name == "IS_NULL":
        # Firestore: a document missing the field does not match.
        return actual is None
    if name == "==":
        return actual == value
    if name == "<=":
        return actual is not None and actual <= value
    raise AssertionError(f"fake does not implement operator {name!r}")


class FakeCollectionReference:
    def __init__(self, client: FakeFirestore, name: str) -> None:
        self.client, self.prefix = client, f"{name}/"

    def document(self, doc_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self.client, f"{self.prefix}{doc_id}", doc_id)

    def where(self, *, filter: Any) -> FakeQuery:
        return FakeQuery(self).where(filter=filter)


class FakeFirestore:
    """``docs`` maps ``"<collection>/<id>"`` to ``(data, update_time)``."""

    # Static on the real client too, so the store's call site is identical.
    write_option = staticmethod(AsyncClient.write_option)

    def __init__(self) -> None:
        self.docs: dict[str, tuple[dict[str, Any], int]] = {}
        # Set by a test to interleave a competing writer; see the docstring.
        self.on_get: Callable[[str], Awaitable[None]] | None = None
        self._clock = 0

    def write(self, path: str, data: dict[str, Any]) -> None:
        self._clock += 1
        self.docs[path] = (data, self._clock)

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, name)
```

- [ ] **Step 2: Smoke the fake before anything depends on it** — a syntax error or a wrong transform surfaces here as two printed lines, not later as a collection error inside a long test module:

```bash
cd anthropic && uv run python - <<'PY'
import asyncio

from google.cloud.firestore_v1.base_query import FieldFilter
from google.cloud.firestore_v1.transforms import ArrayUnion

from tests.fake_firestore import FakeFirestore


async def main() -> None:
    client = FakeFirestore()
    doc = client.collection("c").document("a")
    await doc.create({"webhook_ids": ["one"], "n": None})
    stale = await doc.get()
    await doc.set({"webhook_ids": ArrayUnion(["two"])}, merge=True)
    assert (await doc.get()).to_dict()["webhook_ids"] == ["one", "two"]
    try:
        await doc.update({"x": 1}, option=client.write_option(last_update_time=stale.update_time))
    except Exception as exc:
        print("precondition rejected:", type(exc).__name__)
    query = client.collection("c").where(filter=FieldFilter("n", "==", None))
    print("query:", [s.id async for s in query.stream()])


asyncio.run(main())
PY
```

Expected: `precondition rejected: FailedPrecondition`, then `query: ['a']`. The second line is the `IS_NULL` behaviour: the document matched because it carries `n: None` explicitly, and one lacking the field would not have.

- [ ] **Step 3: Write the tests `upsert` alone has to pass** — `tests/test_store.py`:

```python
"""FirestorePendingStore — the write path: upsert, complete, seen."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from slashid_ai_forwarder_core.events import (
    AIInvocationContent,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)

from slashid_anthropic_forwarder.record import (
    FILE_DIGESTS,
    HOOK,
    MAX_EVENT_BYTES,
    Append,
    PendingRecord,
    event_fields,
)
from slashid_anthropic_forwarder.store import FirestorePendingStore, Seen
from tests.fake_firestore import FakeFirestore

NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
JOIN_WAIT = timedelta(hours=1)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def a_store() -> tuple[FirestorePendingStore, FakeFirestore]:
    client = FakeFirestore()
    return (
        FirestorePendingStore(client=client, collection="anthropic_pending", join_wait=JOIN_WAIT),
        client,
    )


def an_event(**over: object) -> dict[str, object]:
    return {"request_id": ADDRESS, "timestamp": "2026-09-20T23:08:20+00:00"} | over


async def test_upsert_creates_and_reports_readiness() -> None:
    store, client = a_store()
    outcome = await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert (outcome.stored, outcome.created, outcome.ready) == (True, True, True)
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["deadline"] == NOW + JOIN_WAIT
    assert stored["next_attempt_at"] == NOW + JOIN_WAIT
    # Written explicitly: Firestore's IS_NULL filter does not match a
    # document that lacks the field, and `due` depends on it.
    assert stored["tombstoned_at"] is None


async def test_expectations_make_a_record_unready() -> None:
    store, _ = a_store()
    outcome = await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    assert outcome.stored and not outcome.ready


async def test_a_second_delivery_merges_and_never_moves_the_deadline() -> None:
    """43 of 239 measured invocations were revealed by more than one
    delivery; a second one must not extend the wait."""
    store, client = a_store()
    await store.upsert(
        ADDRESS, {"event": an_event(), "webhook_ids": Append(("msg_a",))}, (), now=NOW
    )
    outcome = await store.upsert(
        ADDRESS,
        {"event": an_event(model={"id": "claude-opus-5"}), "webhook_ids": Append(("msg_b",))},
        (),
        now=NOW + timedelta(minutes=20),
    )
    assert outcome.stored and not outcome.created and outcome.ready
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["deadline"] == NOW + JOIN_WAIT
    # Append, not replace: Reader A matches a denial against any of them.
    assert stored["webhook_ids"] == ["msg_a", "msg_b"]
    assert stored["event"]["model"] == {"id": "claude-opus-5"}
```

- [ ] **Step 4: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_store.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.store'`.

- [ ] **Step 5: Implement** `src/slashid_anthropic_forwarder/store.py` — the protocol, the outcome types and `upsert`:

```python
"""The pending store: a port with six operations, and its Firestore adapter.

The receiver never pushes from the request path. It writes a record and
returns; a completing writer or the deadline sweep pushes later. Which
of the two gets to push is settled by ``claim``.

Everything backend-specific stays in the adapter: the document id (the
address — Firestore ids may not contain ``/``, which no address does),
the array transforms, the TTL policy — which keys on
``tombstone_expires_at``, a field only a tombstone carries — the named
database, and the composite index behind ``due`` (``tombstoned_at`` ASC,
``next_attempt_at`` ASC).
Another cloud reimplements six methods and nothing above this line
changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.transforms import ArrayUnion

from .record import Append, PendingRecord


class Seen(StrEnum):
    """Three states, and ``ABSENT`` is the one that matters: it is what lets
    a reader emit an invocation standalone."""

    LIVE = "live"
    TOMBSTONED = "tombstoned"
    ABSENT = "absent"


class Retirement(StrEnum):
    """``PUSHED`` tombstones; ``FAILED`` releases the claim and sets a
    next-attempt time; ``SUPERSEDED`` tombstones without pushing, which is
    how a successor frame discards a tail record."""

    PUSHED = "pushed"
    FAILED = "failed"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class Outcome:
    """What a write did, and whether the record is now ready to push.

    ``stored`` is False when the address is tombstoned — or, for
    ``complete``, when no record exists — which is a no-op, not an error.
    """

    stored: bool
    ready: bool
    created: bool = False


NO_OP = Outcome(stored=False, ready=False)


class PendingStore(Protocol):
    """Six operations. The contracts are the design's, verbatim."""

    async def upsert(
        self,
        address: str,
        fields: dict[str, Any],
        expectations: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        """Create or merge. Creating sets the deadline and seeds
        expectations; merging never moves the deadline. A no-op on a
        tombstoned address, and it says so."""
        ...

    async def complete(
        self,
        address: str,
        fields: dict[str, Any],
        clears: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        """Merge and clear. Never creates. A no-op on a tombstoned address.
        Reports readiness for the same reason ``upsert`` does: a completing
        writer that sees it knows to claim."""
        ...

    async def claim(
        self,
        address: str,
        lease: timedelta,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> PendingRecord | None:
        """Take the exclusive right to push, for a bounded lease, and return
        the record **as it is now**. Every pusher calls it, the flusher
        included. ``None`` when someone else holds the lease, or the record
        is gone or tombstoned."""
        ...

    async def due(self, now: datetime, limit: int) -> list[PendingRecord]:
        """Live records past their deadline whose claim is absent or
        expired, oldest first, bounded."""
        ...

    async def retire(
        self, address: str, outcome: Retirement | str, *, now: datetime | None = None
    ) -> None: ...

    async def seen(self, address: str) -> Seen: ...


class FirestorePendingStore:
    """Firestore-backed ``PendingStore`` — one document per address."""

    def __init__(
        self,
        *,
        client: Any,  # google.cloud.firestore.AsyncClient — untyped as in vertex's store
        collection: str,
        join_wait: timedelta,
        retry_backoff: timedelta = timedelta(seconds=60),
    ) -> None:
        self._client = client
        self._collection = client.collection(collection)
        self._join_wait = join_wait
        self._retry_backoff = retry_backoff

    def _ref(self, address: str) -> Any:
        return self._collection.document(address)

    async def upsert(
        self,
        address: str,
        fields: dict[str, Any],
        expectations: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        now = now or datetime.now(UTC)
        deadline = now + self._join_wait
        try:
            await self._ref(address).create(
                {
                    "deadline": deadline,
                    # Equal on creation; the lease and the backoff move only
                    # this one, so the deadline the record was born with
                    # survives every merge.
                    "next_attempt_at": deadline,
                    "awaiting": list(expectations),
                    "attempts": 0,
                    "claim_owner": None,
                    "claim_expires_at": None,
                    # Written explicitly: an IS_NULL filter does not match a
                    # document that lacks the field, and `due` needs it to.
                    "tombstoned_at": None,
                    # Last, so the caller's fields win: ``event_fields`` sets
                    # ``elided`` only when it actually dropped text, and a
                    # literal after the spread would overwrite it on every
                    # create — which is every frame-built record.
                    **_plain(fields),
                }
            )
            return Outcome(stored=True, ready=not expectations, created=True)
        except AlreadyExists:
            pass
        snapshot = await self._ref(address).get()
        data = snapshot.to_dict() or {}
        if data.get("tombstoned_at") is not None:
            return NO_OP
        merge = _transforms(fields)
        if expectations:
            merge["awaiting"] = ArrayUnion(list(expectations))
        await self._ref(address).set(merge, merge=True)
        after = (await self._ref(address).get()).to_dict() or {}
        return Outcome(stored=True, ready=not after.get("awaiting"))


def _transforms(fields: dict[str, Any]) -> dict[str, Any]:
    """Translate the record module's backend-neutral ``Append`` markers into
    Firestore array transforms."""
    return {
        key: ArrayUnion(list(value.values)) if isinstance(value, Append) else value
        for key, value in fields.items()
    }


def _plain(fields: dict[str, Any]) -> dict[str, Any]:
    """The same fields for a ``create``, where a transform is pointless (and
    an ``ArrayUnion`` against a field that does not exist yet is a write the
    backend has to resolve for nothing)."""
    return {
        key: list(value.values) if isinstance(value, Append) else value
        for key, value in fields.items()
    }
```

- [ ] **Step 6: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_store.py -v`. Expected: `3 passed`. The protocol declares six methods and the adapter has one; nothing asserts conformance yet, so `ty` stays quiet until Task 5.6 adds the annotation that checks it.

- [ ] **Step 7: Write the rest of the write-path tests** — append to `tests/test_store.py`:

```python
async def test_upsert_is_a_no_op_on_a_tombstoned_address() -> None:
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, "pushed", now=NOW)
    outcome = await store.upsert(ADDRESS, {"event": an_event(model={"id": "x"})}, (), now=NOW)
    assert not outcome.stored and not outcome.ready
    assert "model" not in client.docs["anthropic_pending/" + ADDRESS][0]["event"]


async def test_complete_never_creates() -> None:
    """A record that does not exist was never opened by a frame; inventing
    one here would resurrect an invocation that was already pushed."""
    store, client = a_store()
    outcome = await store.complete(ADDRESS, {"event": an_event()}, ())
    assert not outcome.stored
    assert client.docs == {}


async def test_complete_is_a_no_op_on_a_tombstoned_address() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    await store.retire(ADDRESS, "pushed", now=NOW)
    assert not (await store.complete(ADDRESS, {"event": {}}, (FILE_DIGESTS,))).stored


async def test_complete_merges_into_the_event_without_replacing_it() -> None:
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event(input={"byte_length": 9})}, (), now=NOW)
    await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, ())
    event = client.docs["anthropic_pending/" + ADDRESS][0]["event"]
    assert event["input"] == {"byte_length": 9} and event["output"] == {"byte_length": 4}


async def test_two_completers_both_land() -> None:
    """Different fields, neither lost — the merge is per field, not per
    document."""
    store, client = a_store()
    opened = {"event": an_event(), "contributed": Append((HOOK,))}
    await store.upsert(ADDRESS, opened, (FILE_DIGESTS,), now=NOW)
    first = await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, ())
    second = await store.complete(
        ADDRESS,
        {
            "event": {"accessed_files": [{"name": "maria.txt"}]},
            "contributed": Append(("compliance",)),
        },
        (FILE_DIGESTS,),
    )
    assert not first.ready and second.ready
    event = client.docs["anthropic_pending/" + ADDRESS][0]["event"]
    assert event["output"] == {"byte_length": 4}
    assert event["accessed_files"] == [{"name": "maria.txt"}]
    assert client.docs["anthropic_pending/" + ADDRESS][0]["contributed"] == [HOOK, "compliance"]


async def test_a_visit_that_finds_nothing_still_settles_the_record() -> None:
    """Digests are outstanding until a reader visits, not until it finds
    files; otherwise a listing that never materializes waits out the full
    deadline for nothing."""
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    assert (await store.complete(ADDRESS, {}, (FILE_DIGESTS,))).ready


async def test_seen_has_three_states() -> None:
    """Absent is what lets a reader emit standalone."""
    store, _ = a_store()
    assert await store.seen(ADDRESS) is Seen.ABSENT
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert await store.seen(ADDRESS) is Seen.LIVE
    await store.retire(ADDRESS, "pushed", now=NOW)
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_an_oversized_event_is_bounded_before_it_reaches_the_store() -> None:
    """The adapter writes what it is handed; `record.event_fields` is what
    keeps the write under the limit. Asserted here so the division of labour
    is pinned by a test and not only by a docstring."""
    event = AIInvocationObservedV1(
        request_id=ADDRESS,
        timestamp="2026-09-20T23:08:20+00:00",
        identity_details=AnthropicIdentityDetails(user_id="user_01"),
        model=AIModel(id="claude-opus-5"),
        parsed_as="anthropic-inference-hook",
        input=AIInvocationContent(redacted_text="x" * 2_000_000, byte_length=2_000_000),
    )
    store, client = a_store()
    await store.upsert(ADDRESS, event_fields(event), (), now=NOW)
    record: PendingRecord = (await store.due(NOW + timedelta(hours=2), 10))[0]
    assert record.elided
    assert len(str(client.docs["anthropic_pending/" + ADDRESS][0]["event"])) < MAX_EVENT_BYTES
```

- [ ] **Step 8: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_store.py -v`. Expected: `8 failed, 3 passed` — `AttributeError: … has no attribute 'complete'` on most of them, and `'retire'` or `'due'` on the four Task 5.6 finishes.

- [ ] **Step 9: Implement `complete` and `seen`** — widen the transforms import to `from google.cloud.firestore_v1.transforms import ArrayRemove, ArrayUnion` (Step 5 imported only what it used, so `--fix` had nothing to strip), then add both methods to `FirestorePendingStore`, after `upsert` and before the module-level helpers:

```python
    async def complete(
        self,
        address: str,
        fields: dict[str, Any],
        clears: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome:
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return NO_OP
        if (snapshot.to_dict() or {}).get("tombstoned_at") is not None:
            return NO_OP
        merge = _transforms(fields)
        if clears:
            # ArrayRemove, not a rewrite: two readers clearing different
            # expectations must not clobber each other.
            merge["awaiting"] = ArrayRemove(list(clears))
        await self._ref(address).set(merge, merge=True)
        after = (await self._ref(address).get()).to_dict() or {}
        return Outcome(stored=True, ready=not after.get("awaiting"))

    async def seen(self, address: str) -> Seen:
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return Seen.ABSENT
        if (snapshot.to_dict() or {}).get("tombstoned_at") is not None:
            return Seen.TOMBSTONED
        return Seen.LIVE
```

- [ ] **Step 10: Run to verify the write path passes** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_store.py -v -k "not tombstoned and not three_states and not oversized"`. Expected: `7 passed, 4 deselected` — the four that need `retire` or `due` are deselected until the next task implements them.

- [ ] **Step 11: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/store.py anthropic/tests/fake_firestore.py \
        anthropic/tests/test_store.py
git commit -m "feat(anthropic): pending-store protocol and its firestore write path"
```

### Task 5.6: `store.py` — `claim`, `due`, `retire`, and the races

`claim` is the operation the first draft lacked, and every detail of it exists for a measured reason.

**It is a compare-and-set, not a flag test.** Readiness is a *state*: a record born ready — under emit-previous, most of them — has no transition anyone could hook, so "did this call empty the expectation set?" cannot arbitrate, and a flusher and a completer can both observe readiness and push twice. The CAS is `update` under a `last_update_time` precondition, so the loser's write is rejected by the backend rather than by a re-read.

**It is a lease, not a flag.** A push that fails after claiming must not orphan the record, and a crash between claiming and pushing must be recovered rather than leaving a live record nothing will ever collect. Both fall out of one field: `next_attempt_at` is the deadline on creation, the end of the lease while claimed, and the end of the backoff after a failure — so "claim absent or expired" is a single inequality and `due` needs one composite index, not a two-inequality query.

**It returns the record, and the pusher pushes what it returns.** Reading through `due` and pushing that snapshot would drop fields a completer merged in between — precisely the digests the wait exists for. A push is a commitment: the terminal's dedup is first-completed-wins and never tops up.

And `due` **pushes rather than deletes**: two classes of invocation have no compliance counterpart at all (zero-data-retention organizations, and the sub-conversations that share a `session_id`), so a deleted record there is a lost event.

`retire` also writes the field the TTL policy keys on, and it is not `tombstoned_at`. Firestore deletes a document once the timestamp in the nominated field is **in the past**, so a policy pointed at the instant of tombstoning asks for every tombstone to be collected the moment it is written — and the tombstone is the only thing suppressing a late reader's duplicate for the next two hours. The expiry instant goes in its own field, which a live record never carries, so the policy cannot reach one that is still failing to push.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/store.py` (append to the adapter)
- Test: `anthropic/tests/test_store.py` (append)

- [ ] **Step 1: Write the failing tests** — extend the import to `from slashid_anthropic_forwarder.store import FirestorePendingStore, PendingStore, Retirement, Seen`, widen the `a_store` helper so it forwards the store's two durations:

```python
def a_store(
    *, join_wait: timedelta = JOIN_WAIT, tombstone_ttl: timedelta = timedelta(hours=2)
) -> tuple[FirestorePendingStore, FakeFirestore]:
    client = FakeFirestore()
    return (
        FirestorePendingStore(
            client=client,
            collection="anthropic_pending",
            join_wait=join_wait,
            tombstone_ttl=tombstone_ttl,
        ),
        client,
    )
```

and append:

```python
LEASE = timedelta(minutes=5)
TOMBSTONE_TTL = timedelta(hours=2)
PAST_DEADLINE = NOW + JOIN_WAIT + timedelta(minutes=1)


async def test_claim_returns_the_record_as_it_is_now() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    await store.complete(ADDRESS, {"event": {"output": {"byte_length": 4}}}, (FILE_DIGESTS,))
    record = await store.claim(ADDRESS, LEASE, owner="tick-1", now=PAST_DEADLINE)
    assert record is not None
    assert record.address == ADDRESS
    assert record.event["output"] == {"byte_length": 4}
    assert record.ready and record.deadline == NOW + JOIN_WAIT


async def test_exactly_one_of_two_claims_wins() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    first = await store.claim(ADDRESS, LEASE, owner="completer", now=PAST_DEADLINE)
    second = await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    assert first is not None and second is None


async def test_a_completer_that_merges_before_the_sweep_claims_wins_the_digests() -> None:
    """The race the wait exists for: whoever claims pushes, and what they
    push includes everything merged up to that instant. Note the clock —
    the sweep is locked out for the length of the lease, not forever, and
    what keeps it out afterwards is the tombstone the completer left."""
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (FILE_DIGESTS,), now=NOW)
    outcome = await store.complete(
        ADDRESS, {"event": {"accessed_files": [{"content_hashes": {"md5": "d4 1d"}}]}},
        (FILE_DIGESTS,),
    )
    assert outcome.ready
    completer = await store.claim(ADDRESS, LEASE, owner="completer", now=NOW)
    assert completer is not None
    assert completer.event["accessed_files"][0]["content_hashes"] == {"md5": "d4 1d"}
    assert await store.claim(ADDRESS, LEASE, owner="sweep", now=NOW + LEASE / 2) is None
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW + timedelta(minutes=2))
    assert await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE) is None


async def test_a_completion_after_the_claim_still_lands_but_misses_that_push() -> None:
    """Documented, not accidental: a push is a commitment the terminal will
    not top up, which is why JOIN_WAIT sits beyond normal reader lag."""
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    claimed = await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.complete(ADDRESS, {"event": {"accessed_files": [{"name": "late.txt"}]}}, ())
    assert claimed is not None and "accessed_files" not in claimed.event
    assert "accessed_files" in client.docs["anthropic_pending/" + ADDRESS][0]["event"]


async def test_a_claim_that_read_a_stale_snapshot_is_refused_by_the_precondition() -> None:
    """The lease guard is not the arbiter — the compare-and-set is.

    Both pushers read before either writes, so both see no lease and both
    reach the ``update``; only ``last_update_time`` separates them. Every
    other test here returns at the guard three lines earlier, which would
    leave the branch that actually prevents a double push uncovered. The
    interleave is what the fake's ``on_get`` hook exists for."""
    store, client = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)

    async def rival_claims_first(_path: str) -> None:
        client.on_get = None  # one shot; the rival's own read must not recurse
        assert await store.claim(ADDRESS, LEASE, owner="rival", now=PAST_DEADLINE) is not None

    client.on_get = rival_claims_first
    assert await store.claim(ADDRESS, LEASE, owner="loser", now=PAST_DEADLINE) is None
    assert client.docs["anthropic_pending/" + ADDRESS][0]["claim_owner"] == "rival"


async def test_an_expired_lease_can_be_claimed_again() -> None:
    """A crash between claiming and pushing is recovered on a later tick."""
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    assert await store.claim(ADDRESS, LEASE, owner="crashed", now=PAST_DEADLINE) is not None
    later = PAST_DEADLINE + LEASE + timedelta(seconds=1)
    recovered = await store.claim(ADDRESS, LEASE, owner="next-tick", now=later)
    assert recovered is not None and recovered.claim_owner == "next-tick"


async def test_claim_refuses_an_absent_or_tombstoned_address() -> None:
    store, _ = a_store()
    assert await store.claim(ADDRESS, LEASE, owner="x", now=NOW) is None
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW)
    assert await store.claim(ADDRESS, LEASE, owner="x", now=PAST_DEADLINE) is None


async def test_due_returns_oldest_first_and_honours_its_bound() -> None:
    store, _ = a_store()
    for minutes in (30, 10, 20):
        opened_at = NOW + timedelta(minutes=minutes)
        await store.upsert(f"toolu_{minutes}", {"event": {}}, (), now=opened_at)
    due = await store.due(NOW + timedelta(hours=2), 2)
    assert [r.address for r in due] == ["toolu_10", "toolu_20"]


async def test_due_excludes_the_not_yet_deadlined_the_leased_and_the_tombstoned() -> None:
    store, _ = a_store()
    await store.upsert("toolu_waiting", {"event": {}}, (), now=NOW)
    await store.upsert("toolu_leased", {"event": {}}, (), now=NOW)
    await store.upsert("toolu_gone", {"event": {}}, (), now=NOW)
    await store.claim("toolu_leased", LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire("toolu_gone", Retirement.PUSHED, now=PAST_DEADLINE)
    assert await store.due(NOW + timedelta(minutes=30), 10) == []
    assert [r.address for r in await store.due(PAST_DEADLINE, 10)] == ["toolu_waiting"]


async def test_a_failed_push_releases_the_lease_and_due_returns_it_again() -> None:
    store, _ = a_store()
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE)
    assert await store.due(PAST_DEADLINE, 10) == []  # backing off, not orphaned
    retried = await store.due(PAST_DEADLINE + timedelta(minutes=2), 10)
    assert [r.address for r in retried] == [ADDRESS]
    assert retried[0].attempts == 1
    assert retried[0].claim_owner is None and retried[0].claim_expires_at is None
    # Backoff grows with attempts, so a persistently failing sink is not a
    # hot loop against the terminal.
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE + timedelta(minutes=2))
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE + timedelta(minutes=2))
    assert await store.due(PAST_DEADLINE + timedelta(minutes=3), 10) == []


async def test_retire_superseded_tombstones_without_pushing() -> None:
    """How a successor frame discards the tail record it reconstructed."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    tail = "tail:" + "0" * 64
    await store.upsert(tail, {"event": an_event()}, (), now=NOW)
    await store.retire(tail, Retirement.SUPERSEDED, now=NOW)
    assert await store.seen(tail) is Seen.TOMBSTONED
    assert await store.due(PAST_DEADLINE, 10) == []
    # Same clock as a pushed one: a discarded tail suppresses nothing, but
    # letting it expire on a different schedule is one more thing to reason
    # about for no gain.
    stored = client.docs["anthropic_pending/" + tail][0]
    assert stored["tombstone_expires_at"] == NOW + TOMBSTONE_TTL


async def test_retire_is_harmless_on_an_address_that_is_gone() -> None:
    store, _ = a_store()
    await store.retire(ADDRESS, Retirement.FAILED, now=NOW)
    assert await store.seen(ADDRESS) is Seen.ABSENT


async def test_a_tombstone_carries_the_instant_the_ttl_policy_deletes_it() -> None:
    """Firestore deletes a document once the nominated field is in the
    past, so the field holds the expiry, not the moment of tombstoning —
    keying the policy on ``tombstoned_at`` would ask for every tombstone to
    be collected the moment it is written, and the suppression it exists
    for would last only as long as the deletion lag."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW)
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["tombstoned_at"] == NOW
    assert stored["tombstone_expires_at"] == NOW + TOMBSTONE_TTL


async def test_a_live_record_is_invisible_to_the_ttl_policy() -> None:
    """A record that has been failing to push for two hours must not be
    deleted out from under the sweep — which is precisely the loss the
    store exists to prevent."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE)
    assert "tombstone_expires_at" not in client.docs["anthropic_pending/" + ADDRESS][0]


def test_the_adapter_satisfies_the_port() -> None:
    """Checked by `ty`, not at runtime: the annotation is what makes a
    missing or re-signed method a type error, which is the only thing
    keeping the protocol honest now that six methods exist."""
    port: PendingStore = a_store()[0]
    assert port is not None
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_store.py -v`. Expected: `26 failed` — the widened `a_store` is shared, so every test in the file now stops at `TypeError: FirestorePendingStore.__init__() got an unexpected keyword argument 'tombstone_ttl'`. That the whole file goes red here is the point of taking the constructor change in the same step as the tests that need it: after Step 3 the count goes straight back up rather than leaving a half-widened helper behind. `uv run ty check` fails too, on the protocol-conformance annotation — three methods are declared on the port and missing from the adapter.

- [ ] **Step 3: Implement** — first widen the imports with the four names only this half uses (Task 5.5 left them out so its `ruff check --fix` had nothing to strip):

```python
from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore_v1.base_query import FieldFilter

from .record import Append, PendingRecord, from_document
```

then add the constructor's second duration beside `join_wait`:

```python
        # How long a tombstone suppresses a late reader's duplicate. It must
        # exceed JOIN_WAIT + POLL_LAG + one tick; the startup assertion that
        # enforces the inequality lands with the tick cadence in the deploy
        # chunk, and the default matches SLASHID_TOMBSTONE_TTL_SECONDS.
        tombstone_ttl: timedelta = timedelta(hours=2),
```
```python
        self._tombstone_ttl = tombstone_ttl
```

and append the three methods to `FirestorePendingStore`, after `complete` and before `seen`:

```python
    async def claim(
        self,
        address: str,
        lease: timedelta,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> PendingRecord | None:
        """Take the exclusive right to push, and return the record as it is.

        Compare-and-set on the snapshot's ``update_time``: any write between
        the read and the claim — another claimer, or a completer merging
        digests — invalidates the precondition and this caller backs off.
        Readiness is deliberately not checked here: the deadline sweep
        claims records that will never become ready, and flushing emits
        whatever the record holds.
        """
        now = now or datetime.now(UTC)
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        if data.get("tombstoned_at") is not None:
            return None
        held = data.get("claim_expires_at")
        if held is not None and held > now:
            return None
        expires = now + lease
        try:
            await self._ref(address).update(
                {
                    "claim_owner": owner,
                    "claim_expires_at": expires,
                    # The lease IS the next-attempt time: a crash before
                    # `retire` leaves the record collectable at `expires`
                    # rather than orphaned.
                    "next_attempt_at": expires,
                },
                option=self._client.write_option(last_update_time=snapshot.update_time),
            )
        except (FailedPrecondition, NotFound):
            return None
        return from_document(
            address,
            {
                **data,
                "claim_owner": owner,
                "claim_expires_at": expires,
                "next_attempt_at": expires,
            },
        )

    async def due(self, now: datetime, limit: int) -> list[PendingRecord]:
        """Live records ready for a pusher, oldest first, bounded.

        One inequality, because ``next_attempt_at`` already folds in the
        lease and the backoff. The composite index this needs is
        ``tombstoned_at`` ASC, ``next_attempt_at`` ASC; the Terraform
        provisions it.
        """
        query = (
            self._collection.where(filter=FieldFilter("tombstoned_at", "==", None))
            .where(filter=FieldFilter("next_attempt_at", "<=", now))
            .order_by("next_attempt_at")
            .limit(limit)
        )
        return [
            from_document(snapshot.id, snapshot.to_dict() or {})
            async for snapshot in query.stream()
        ]

    async def retire(
        self, address: str, outcome: Retirement | str, *, now: datetime | None = None
    ) -> None:
        """Close a record out. ``FAILED`` is the only outcome that keeps it
        alive, and it releases the lease so a later tick can collect it."""
        now = now or datetime.now(UTC)
        if Retirement(outcome) is not Retirement.FAILED:
            # Push, then retire: a crash between them re-pushes an event the
            # terminal dedups, where the reverse order loses it outright.
            #
            # ``tombstone_expires_at`` is what the TTL policy keys on, and it
            # holds the expiry instant rather than the moment of tombstoning:
            # Firestore deletes once the nominated field is in the past, so a
            # policy pointed at ``tombstoned_at`` would collect every
            # tombstone as it was written. No live record carries the field,
            # so the policy cannot reach one.
            await self._ref(address).set(
                {
                    "tombstoned_at": now,
                    "tombstone_expires_at": now + self._tombstone_ttl,
                    "claim_owner": None,
                    "claim_expires_at": None,
                },
                merge=True,
            )
            return
        snapshot = await self._ref(address).get()
        if not snapshot.exists:
            return
        attempts = int((snapshot.to_dict() or {}).get("attempts") or 0) + 1
        await self._ref(address).set(
            {
                "attempts": attempts,
                "claim_owner": None,
                "claim_expires_at": None,
                "next_attempt_at": now + self._retry_backoff * attempts,
            },
            merge=True,
        )
```

Note `retire(PUSHED)` uses `set(merge=True)` rather than `update`: a tombstone must land even if the record was never created, which is how Reader B's standalone emission leaves a suppressing sentinel for an address it only ever pushed through.

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_store.py -v && uv run ty check`. Expected: `26 passed` (11 from Task 5.5, 15 here). If `test_due_returns_oldest_first_and_honours_its_bound` returns nothing, the cause is the `IS_NULL` behaviour in the fake's `_matches`, not the query.

- [ ] **Step 5: Run the whole subproject** — `cd anthropic && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run ty check`. Expected: the chunk adds 61 tests (22 in `test_address.py`, 13 in `test_record.py`, 26 in `test_store.py`) to whatever Chunks 1–4 left passing; ruff and ty clean.

- [ ] **Step 6: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/store.py anthropic/tests/test_store.py
git commit -m "feat(anthropic): claim leases, the deadline sweep and retirement"
```

---

---

## Chunk 6: Readiness, the flush, and the entrypoint

Chunks 3 to 5 built the pieces a delivery turns into storage: a frame, a verdict, an address, a record and a store. This chunk is the only place they meet. `pending.py` answers two questions — what one frame writes (the previous run's record, the fresh round's tail, and the predecessor tail it discards) and what one tick flushes — and `main.py` rewires the HTTP surface around them, adding `POST /tick` beside the catch-all hook route and finally handing the app a real `FirestorePendingStore`, which Chunk 5 deliberately left unwired because nothing in it reads `Config`. Two design rules govern almost every decision here. **Readiness is a state, not a transition**: under emit-previous most records are complete the moment they are created, so nobody observes them *becoming* ready and a plan that pushed on the transition would leave them all sitting until the deadline; every pusher therefore arbitrates the same way, by winning `claim`, and pushes what `claim` handed back rather than an older snapshot. And **an eventing failure must never become a verdict failure**: the receiver answers Anthropic first and writes afterwards, in a background task whose outcome cannot reach the response, because a non-200 is a webhook failure and enough of them trip Anthropic's circuit breaker and disable enforcement outright.

Everything below calls Chunk 5's modules by their real names — `address.joinable_address` / `hook_address` / `tail_address`, `record.open_fields` / `to_event` / `FILE_DIGESTS` / `HOOK`, and `store.PendingStore` / `PendingRecord` / `Outcome` / `Retirement` / `FirestorePendingStore` — and its fake Firestore client at `tests/fake_firestore.py`, so this chunk adds no second test double for the same protocol. One function is added to `address.py`, in Task 6.2, for a collision named there.

### Task 6.1: the config fields the store and the flush need

Chunk 5's `FirestorePendingStore` takes a client, a collection and a `join_wait` and reads no configuration of its own, which is the right shape for a port and leaves every one of its knobs unnamed. This task names them, plus the one the `awaiting` seeding rule needs: a record may wait for attachment digests only when a reader exists to deliver them, so `compliance_enabled` is consulted on the request path even though the compliance credential itself belongs to a later chunk. Without that check a hook-only deployment would seed an expectation nothing could ever clear, and every attachment-bearing round would wait out the full `JOIN_WAIT` before flushing exactly what it already held.

`SLASHID_TOMBSTONE_TTL_SECONDS` is declared here and enforced nowhere yet: the startup assertion the design asks for (`TOMBSTONE_TTL > JOIN_WAIT + POLL_LAG + one tick`) cannot be written until the tick cadence is a declared input, which happens in the deploy chunk. The field exists now so the Terraform and the TTL policy have one name to refer to.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/config.py`
- Test: `anthropic/tests/test_config.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`, reusing that module's own config builder:

```python
def test_compliance_is_off_without_a_key() -> None:
    assert _config().compliance_enabled is False


def test_a_compliance_key_turns_the_readers_on() -> None:
    assert _config(compliance_key="sk-ant-api01-x").compliance_enabled is True


def test_the_store_knobs_have_the_designed_defaults() -> None:
    config = _config()
    assert config.join_wait_seconds == 3600
    assert config.tombstone_ttl_seconds == 7200
    assert config.pending_collection == "anthropic_pending"
    assert config.firestore_database == "slashid-anthropic"
    assert config.max_flushes_per_tick == 500
    assert config.gcp_project_id is None
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_config.py -v`. Expected: three failures, the first `AttributeError: 'Config' object has no attribute 'compliance_enabled'`.

- [ ] **Step 3: Implement** — add to `Config`, after `capture_deny_marker`:

```python
    # sk-ant-api01-…. Setting it enables the compliance readers; the
    # readers chunk adds the rest of their configuration. It is read on
    # the request path for one reason: a record may only wait for
    # attachment digests when something exists to deliver them.
    compliance_key: str | None = None
    # The project holding Firestore; vertex/ has the same field.
    gcp_project_id: str | None = None
    # The named database, as vertex/ names its own slashid-vertex rather
    # than using (default).
    firestore_database: str = "slashid-anthropic"
    # Collection holding pending records and their tombstones.
    pending_collection: str = "anthropic_pending"
    # Deadline before an unsettled record is pushed as it stands.
    join_wait_seconds: int = 3_600
    # How long a pushed record's tombstone suppresses a late reader's
    # duplicate. Must exceed JOIN_WAIT + POLL_LAG + one tick; the
    # assertion lands with the tick cadence in the deploy chunk.
    tombstone_ttl_seconds: int = 7_200
    # Bounds `due` so one tick cannot stall behind a backlog.
    max_flushes_per_tick: int = 500

    @property
    def compliance_enabled(self) -> bool:
        return bool(self.compliance_key)
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_config.py -v && uv run ty check`. Expected: the module's existing tests plus 3, all passing.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/config.py anthropic/tests/test_config.py
git commit -m "feat(anthropic): store knobs and the compliance capability flag"
```

### Task 6.2: `address.py` — `deny_address`, and why the prefix exists

A fourth address, extending the module Chunk 5 created. One frame can carry **both** an unjoinable previous run and an honoured deny on its fresh round: the previous run has no `tool_use.id`, so it falls back to `hook:` plus the delivery id, and the denial has no successor frame that could ever address it, so it too can only be keyed on the delivery. Two different invocations, one key — and `upsert`'s merge semantics would fold them into a single record whose `event` is whichever writer went second. The prefix separates them while keeping the denial keyed on exactly the value Reader A gets from an activity's `request_id`, so the reader computes `deny_address(activity.request_id)` and finds it.

The combination is not exotic: 90 of 284 measured trailing runs had no tool call, and under enforcement denials are sticky — every subsequent turn in the session is denied, each on its own delivery — so an enforcing tenant produces frames of exactly this shape in a run.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/address.py` (append)
- Test: `anthropic/tests/test_address.py` (append)

- [ ] **Step 1: Write the failing tests** — extend that module's import to include `deny_address` and append:

```python
def test_a_denial_is_keyed_on_its_delivery() -> None:
    assert deny_address("msg_1") == "deny:msg_1"


def test_a_denial_and_an_unjoinable_run_on_one_delivery_do_not_collide() -> None:
    """Both can arrive on the same frame, and merging them would leave one
    record holding the other's event."""
    assert deny_address("msg_1") != hook_address("msg_1")
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_address.py -v`. Expected: collection ERROR, `ImportError: cannot import name 'deny_address' from 'slashid_anthropic_forwarder.address'`.

- [ ] **Step 3: Implement** — append to `address.py`, beside `hook_address`:

```python
def deny_address(webhook_id: str) -> str:
    """The address of a round whose deny was honoured: ``deny:`` + the delivery id.

    Keyed on the delivery, which is what the denial activity names, but
    under its own prefix. One frame can carry an unjoinable previous run
    *and* an honoured deny on its fresh round — two invocations, and
    ``hook:`` for both would merge them into one record.
    """
    return f"deny:{webhook_id}"
```

and extend the module docstring's list of keys with a fourth entry.

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_address.py -v && uv run ty check`. Expected: Chunk 5's 22 plus 2 = `24 passed`.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/address.py anthropic/tests/test_address.py
git commit -m "feat(anthropic): a distinct address for an honoured denial"
```

### Task 6.3: `pending.py` — claiming, pushing, and the deadline flush

The push path is three short functions and one race. `claim` is the race: a record can be pushed by the writer that made it ready or by the tick that finds it past its deadline, and under first-completed-wins dedup the loser's copy is discarded whole — so which of the two goes first decides whether the invocation lands with its attachment digests. Chunk 5 settled the arbitration with a compare-and-set on `update_time`; this chunk's job is to make sure both pushers actually go through it, and that the winner pushes what `claim` returned rather than the snapshot `due` handed out.

Two properties of Chunk 5's store shape the code. `claim` deliberately does **not** check readiness or the deadline — only the lease — so a record born ready can be claimed and pushed by its creator the moment it is written, which under emit-previous is nearly every record. And `next_attempt_at` is a single field carrying the deadline, the lease and the retry backoff in turn, so `retire(FAILED)` releasing a claim and a crashed pusher's lease lapsing are the same recovery through the same one inequality in `due`. Note what that implies for an early push: claiming moves `next_attempt_at` forward to the end of the lease, so a record that fails its first push becomes collectable after the backoff rather than at its original deadline. That is correct rather than merely convenient — `push_if_ready` only fires on a record with an empty expectation set, so there is nothing left for the shortened wait to lose.

Push happens before retire, always: a crash between them re-pushes an event the terminal's dedup drops, while retiring first loses it outright, and a missing audit record is the failure this product exists to prevent.

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/pending.py`
- Test: `anthropic/tests/test_pending.py`

- [ ] **Step 1: Write the failing tests** — `tests/test_pending.py`. It uses Chunk 5's fake Firestore client behind the real `FirestorePendingStore`, so the flush is tested against the adapter that will actually run rather than against a second in-memory reimplementation of the same six operations.

```python
"""Claiming, pushing, and the deadline flush."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.pending import LEASE, flush_due, push_if_ready
from slashid_anthropic_forwarder.record import HOOK, PARSED_AS_HOOK, open_fields
from slashid_anthropic_forwarder.store import FirestorePendingStore, Seen
from tests.fake_firestore import FakeFirestore

JOIN_WAIT = timedelta(hours=1)
NOW = datetime(2026, 9, 20, 23, 8, 20, tzinfo=UTC)
ADDRESS = "toolu_01Dqhr2d1w2UCUqbXhCSGutC"


def config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.com",
        "push_token": "tok",
        "hook_signing_secret": "whsec_AAAA",
        "max_retries": 0,
    }
    base.update(overrides)
    return Config(**base)


def a_store(**over: Any) -> FirestorePendingStore:
    # A zero backoff so `retire(FAILED)` makes the record collectable at
    # once; the real default is 60 s per attempt.
    kwargs: dict[str, Any] = {
        "client": FakeFirestore(),
        "collection": "anthropic_pending",
        "join_wait": JOIN_WAIT,
        "retry_backoff": timedelta(0),
    }
    return FirestorePendingStore(**(kwargs | over))


def an_event(address: str = ADDRESS) -> AIInvocationObservedV1:
    return AIInvocationObservedV1(
        request_id=address,
        timestamp="2026-09-20T23:08:20+00:00",
        identity_details=AnthropicIdentityDetails(user_id="user_01A"),
        model=AIModel(id="claude-opus-5", provider="anthropic"),
        parsed_as=PARSED_AS_HOOK,
    )


class Sink:
    """Counts pushes and hands back the wire bodies."""

    def __init__(self, *, status: int = 200) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.status = status

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.bodies.append(json.loads(request.content))
            return httpx.Response(self.status, json={})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))

    @property
    def request_ids(self) -> list[str]:
        return [e["request_id"] for body in self.bodies for e in body["events"]]


async def seed(store: FirestorePendingStore, address: str = ADDRESS, *expect: str) -> Any:
    fields = open_fields(an_event(address), webhook_id="msg_1", contributed=HOOK)
    return await store.upsert(address, fields, expect, now=NOW)


async def test_a_record_born_ready_is_pushed_by_its_creator() -> None:
    """Most records are complete on arrival, so nothing ever observes them
    becoming ready; waiting for the deadline would delay nearly every event
    by up to JOIN_WAIT."""
    store, sink = a_store(), Sink()
    outcome = await seed(store)
    async with sink.client() as client:
        pushed = await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client)
    assert pushed is True
    assert sink.request_ids == [ADDRESS]
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_a_waiting_record_is_left_for_the_reader() -> None:
    store, sink = a_store(), Sink()
    outcome = await seed(store, ADDRESS, "file_digests")
    async with sink.client() as client:
        pushed = await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client)
    assert pushed is False
    assert sink.bodies == []
    assert await store.seen(ADDRESS) is Seen.LIVE


async def test_exactly_one_of_two_pushers_wins_the_claim() -> None:
    """A completing writer and the deadline sweep can both observe
    readiness. Under first-completed-wins the loser's copy is discarded
    whole, so the arbitration decides whether the digests land — not
    merely how many requests are made."""
    store, sink = a_store(), Sink()
    outcome = await seed(store)
    async with sink.client() as client:
        results = await asyncio.gather(
            push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client),
            push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client),
        )
    assert sorted(results) == [False, True]
    assert sink.request_ids == [ADDRESS]


async def test_a_failed_push_releases_the_claim_and_comes_back_due() -> None:
    store, failing, ok = a_store(), Sink(status=500), Sink()
    outcome = await seed(store)
    async with failing.client() as client:
        assert await push_if_ready(ADDRESS, outcome, store=store, config=config(), client=client) is False
    assert await store.seen(ADDRESS) is Seen.LIVE  # never landed, so never tombstoned
    later = datetime.now(UTC) + timedelta(seconds=1)
    assert [r.address for r in await store.due(later, 10)] == [ADDRESS]
    async with ok.client() as client:
        assert await flush_due(store, config=config(), client=client, now=later) == 1
    assert ok.request_ids == [ADDRESS]
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_a_claimed_record_is_invisible_to_the_sweep_until_the_lease_lapses() -> None:
    store, sink = a_store(), Sink()
    await seed(store)
    past = NOW + JOIN_WAIT + timedelta(minutes=1)
    assert await store.claim(ADDRESS, LEASE, owner="someone", now=past) is not None
    async with sink.client() as client:
        assert await flush_due(store, config=config(), client=client, now=past + LEASE / 2) == 0
        assert await flush_due(store, config=config(), client=client, now=past + LEASE * 2) == 1


async def test_the_flush_pushes_what_claim_returned_not_what_due_returned() -> None:
    """A completer can merge between the two reads, and a push is a
    commitment the terminal will never top up."""

    class LateCompleter(FirestorePendingStore):
        async def claim(self, address: str, lease: timedelta, **kwargs: Any) -> Any:
            await self.complete(address, {"event": {"request_id": "enriched"}}, ())
            return await super().claim(address, lease, **kwargs)

    store = LateCompleter(
        client=FakeFirestore(), collection="anthropic_pending", join_wait=JOIN_WAIT
    )
    sink = Sink()
    await seed(store)
    async with sink.client() as client:
        await flush_due(store, config=config(), client=client, now=NOW + JOIN_WAIT * 2)
    assert sink.request_ids == ["enriched"]


async def test_the_sweep_is_bounded_per_tick() -> None:
    store, sink = a_store(), Sink()
    for i in range(5):
        await seed(store, f"toolu_{i}")
    async with sink.client() as client:
        flushed = await flush_due(
            store, config=config(max_flushes_per_tick=2), client=client, now=NOW + JOIN_WAIT * 2
        )
    assert flushed == 2
    assert len(sink.request_ids) == 2
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_pending.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.pending'`.

- [ ] **Step 3: Implement** the first half of `pending.py`:

```python
"""What a frame writes, and what a tick flushes.

The receiver never pushes from the request path: a frame writes records
and returns. The one apparent exception is not one — a write that leaves
a record ready pushes it, because readiness is a state rather than a
transition and a record born ready (under emit-previous, most of them)
would otherwise sit until its deadline with nobody to notice. Every
pusher arbitrates the same way: it claims, and the winner pushes what the
claim handed back.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from slashid_ai_forwarder_core.sink import push_invocations

from .config import Config
from .record import PendingRecord, to_event
from .store import Outcome, PendingStore, Retirement

log = logging.getLogger(__name__)

# A push is bounded by ``push_budget_ms`` and the sink's own retries, so the
# lease only has to outlive that. It also bounds recovery: a pusher that
# dies between claiming and pushing leaves its record invisible to ``due``
# until the lease lapses, which costs one tick of latency and no event.
LEASE = timedelta(seconds=60)

# Diagnostic only. The compare-and-set on the document's update time is
# what actually arbitrates, so two pushers sharing an owner string is a
# readability problem and never a correctness one.
WRITER = "writer"
SWEEP = "sweep"


async def push_claimed(
    record: PendingRecord, *, store: PendingStore, config: Config, client: httpx.AsyncClient
) -> bool:
    """Push a record whose claim this caller holds, then retire it.

    Push then retire, never the reverse: a crash between them re-pushes an
    event the terminal's dedup drops, while retiring first loses it
    outright. Validation happens here rather than at write time because
    this is the one path that can log it and retry — the stored document
    is a serialized mapping, and ``to_event`` is where ``parsed_as`` is
    finally decided from ``contributed``.
    """
    try:
        event = to_event(record)
        await push_invocations(
            client,
            [event],
            endpoint=config.endpoint,
            push_token=config.push_token,
            max_retries=config.max_retries,
        )
    except Exception:
        log.exception("push failed for %s; releasing the claim", record.address)
        await store.retire(record.address, Retirement.FAILED)
        return False
    await store.retire(record.address, Retirement.PUSHED)
    return True


async def push_if_ready(
    address: str,
    outcome: Outcome,
    *,
    store: PendingStore,
    config: Config,
    client: httpx.AsyncClient,
) -> bool:
    """Push now if this write left the record ready, and won the claim.

    Both ``upsert`` and ``complete`` report readiness for exactly this
    reason. A lost claim is not a failure: it means the sweep, or another
    writer, is already pushing this record.
    """
    if not outcome.ready:
        return False
    record = await store.claim(address, LEASE, owner=WRITER)
    if record is None:
        return False
    return await push_claimed(record, store=store, config=config, client=client)


async def flush_due(
    store: PendingStore,
    *,
    config: Config,
    client: httpx.AsyncClient,
    now: datetime | None = None,
) -> int:
    """Push every record past its deadline. The tick's job, never a request's.

    Expiry pushes rather than deletes: two classes of invocation have no
    compliance counterpart at all — zero-data-retention organizations, and
    the sub-conversations that share a ``session_id`` — so a deleted
    record there is a lost event. A flush emits whatever the record holds,
    which for a tail is usually input alone, and for one that was waiting
    on digests already includes the output a successor frame supplied.
    """
    moment = now or datetime.now(UTC)
    pushed = 0
    for stale in await store.due(moment, config.max_flushes_per_tick):
        # Claim again and push what *that* returns: a completer may have
        # merged the digests since ``due`` took its snapshot, and a push is
        # a commitment that cannot be topped up later.
        record = await store.claim(stale.address, LEASE, owner=SWEEP, now=moment)
        if record is None:
            continue
        if await push_claimed(record, store=store, config=config, client=client):
            pushed += 1
    log.info("flush: %d records pushed", pushed)
    return pushed
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_pending.py -v && uv run ty check`. Expected: `7 passed`, ruff and ty clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/pending.py anthropic/tests/test_pending.py
git commit -m "feat(anthropic): claim-arbitrated pushes and the deadline flush"
```

### Task 6.4: `pending.py` — what one frame writes

One delivery writes up to three things, and the third has two forms.

1. **The previous run's record.** Its address is `joinable_address(split.assistant_run)` when the run holds a `tool_use`, and `hook_address(webhook_id)` when it does not — an unjoinable run has no key the two sources could ever agree on, so it gets one that needs no agreement and Reader B leaves it alone. The event is Chunk 3's `partial_event`, complete on arrival.
2. **The predecessor's tail, discarded.** Dropping this frame's own trailing assistant run and the round that follows it reconstructs the previous delivery's transcript exactly — that is `split.before` — so its `tail:` key falls out with no per-session pointer, which could not have worked anyway across the hundred-plus sub-conversations sharing one `session_id`. `retire(SUPERSEDED)` tombstones it unpushed. It is a `set(merge=True)`, so it lands even on an address that was never created: if the predecessor frame is still in flight, or was never delivered, the tombstone is waiting and its late `upsert` is a no-op. Out-of-order deliveries resolve themselves.
3. **This frame's own fresh round**, as a tail — or, when the deny was honoured, as the denial itself. Same content either way: input-only, because nothing has answered it. What differs is the address, the `stop_reason`, and whether it may push. A tail has no expectations and so is ready the moment it is written, and it is the one record that must still wait: pushing it would emit every round twice, once as a tail and once as the previous-run record the next frame writes. A denial has no successor at all, so it pushes like any other ready record.

The `awaiting` set is seeded with `FILE_DIGESTS` only when all three of the design's conditions hold: the **attributed** round carries an attachment (the round `A(n-1)` consumed, which is the last round of `split.before` — never the fresh round, whose attachment belongs to the tail), compliance is enabled, and the run is joinable. Miss any one and the expectation could never be cleared. Attachment *blocks* are what is checked, not `accessed_files`: an image attachment carries no extracted text and so yields no entry, yet its stored bytes are precisely what a reader can digest and a frame cannot.

A tail and a denial never seed expectations at all, since neither names a completed run and so neither can be joinable.

Note what needs no special case. A shadow-mode deny is `decision.blocked == False`: the request ran, a successor frame arrives, and the record completes as the ordinary allowed invocation it turned out to be. The `guardrail_intervened` stamp comes from `decision.answered` and is persisted now, because a flush can run an hour later across a redeploy or a mixed-revision rollout, and re-reading `SLASHID_SHADOW_MODE` then would describe a configuration that never applied to this call.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/pending.py`
- Test: `anthropic/tests/test_pending.py`, `anthropic/tests/test_write_from_frame.yaml`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_pending.py`, adding `pathlib`, `pydantic.BaseModel`, `yaml_pytest`, the frame schema and the new names:

```python
from slashid_anthropic_forwarder.address import tail_address
from slashid_anthropic_forwarder.hook.checks import Decision, Verdict
from slashid_anthropic_forwarder.hook.frame import PromptFrame
from slashid_anthropic_forwarder.pending import write_from_frame

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SIGNED_AT = 1758409700
ALLOWED = Decision(composed=Verdict("allow"), answered=Verdict("allow"))
SHADOW_DENY = Decision(composed=Verdict("deny", source="policy"), answered=Verdict("allow"))
BLOCKED = Decision(
    composed=Verdict("deny", source="policy"), answered=Verdict("deny", source="policy")
)
DECISIONS = {"allow": ALLOWED, "shadow_deny": SHADOW_DENY, "blocked": BLOCKED}


def frame(name: str, append: list[dict[str, Any]] | None = None) -> PromptFrame:
    f = PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))
    if not append:
        return f
    extra = [AnthropicRequestMessage.model_validate(m) for m in append]
    return f.model_copy(update={"messages": [*f.messages, *extra]})


def addresses(store: FirestorePendingStore) -> set[str]:
    """Live and tombstoned alike, straight out of the fake's documents."""
    return {path.split("/", 1)[1] for path in store._client.docs}


def document(store: FirestorePendingStore, address: str) -> dict[str, Any]:
    """The raw stored document. Read through the fake rather than through
    ``claim``, because a record that was pushed is already tombstoned and
    ``claim`` would answer ``None`` for it."""
    return store._client.docs[f"anthropic_pending/{address}"][0]


def awaiting(store: FirestorePendingStore, address: str) -> list[str]:
    return sorted(document(store, address).get("awaiting") or [])


async def write(
    f: PromptFrame,
    store: FirestorePendingStore,
    *,
    decision: Decision = ALLOWED,
    webhook_id: str = "msg_1",
    **cfg: Any,
) -> Sink:
    sink = Sink()
    async with sink.client() as client:
        await write_from_frame(
            f,
            decision=decision,
            webhook_id=webhook_id,
            signed_at=SIGNED_AT,
            store=store,
            config=config(**cfg),
            client=client,
        )
    return sink


class Expected(BaseModel):
    record: str | None = None  # the previous run's address, or null for none
    tail: bool = True  # a tail written for the fresh round
    deny: bool = False  # a denial record instead of a tail
    awaiting: list[str] = []


@yaml_pytest(filename="test_write_from_frame.yaml")
async def test_write_from_frame(
    fixture: str,
    append: list[dict[str, Any]],
    decision: str,
    compliance: bool,
    expected: Expected,
) -> None:
    store = a_store()
    extra = {"compliance_key": "sk-ant-api01-x"} if compliance else {}
    await write(frame(fixture, append), store, decision=DECISIONS[decision], **extra)
    written = addresses(store)
    tails = {a for a in written if a.startswith("tail:")}
    denials = {a for a in written if a.startswith("deny:")}
    assert written - tails - denials == ({expected.record} if expected.record else set())
    assert bool(tails) is expected.tail
    assert bool(denials) is expected.deny
    if expected.record:
        assert awaiting(store, expected.record) == expected.awaiting


async def test_an_unjoinable_run_is_addressed_on_the_delivery() -> None:
    """No toolu_ id means no key the two sources could agree on, so it gets
    one that needs no agreement and the reader never emits under it."""
    store = a_store()
    await write(frame("frame_after_shadow_deny"), store, webhook_id="msg_7")
    assert "hook:msg_7" in addresses(store)


async def test_an_unjoinable_run_and_a_denial_on_one_frame_stay_separate() -> None:
    """The collision deny_address exists to prevent: both want the delivery
    id, and a merge would leave one record holding the other's event."""
    store = a_store()
    await write(frame("frame_after_shadow_deny"), store, decision=BLOCKED, webhook_id="msg_7")
    assert {"hook:msg_7", "deny:msg_7"} <= addresses(store)
    run = document(store, "hook:msg_7")["event"]
    denial = document(store, "deny:msg_7")["event"]
    assert run.get("stop_reason") != "guardrail_intervened"
    assert denial["stop_reason"] == "guardrail_intervened"
    assert run["input"] != denial["input"]


async def test_the_successor_discards_the_tail_its_predecessor_wrote() -> None:
    store = a_store()
    first = frame("frame_tool_result")
    await write(first, store, webhook_id="msg_1")
    key = tail_address(list(first.messages), first.session_id)
    assert await store.seen(key) is Seen.LIVE

    # The next delivery: the model answered, and the user replied.
    second = frame(
        "frame_tool_result",
        [
            {"role": "assistant", "content": [{"type": "text", "text": "the first line is…"}]},
            {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
        ],
    )
    sink = await write(second, store, webhook_id="msg_2")
    assert await store.seen(key) is Seen.TOMBSTONED
    assert key not in sink.request_ids  # superseded, never pushed


async def test_a_redundant_delivery_writes_no_second_record() -> None:
    """45 of 492 deliveries carried a transcript already seen: a trailing
    assistant run stays trailing until the model produces a new one."""
    store = a_store()
    f = frame("frame_tool_result")
    await write(f, store, webhook_id="msg_1")
    before = addresses(store)
    await write(f, store, webhook_id="msg_2")
    assert addresses(store) == before


async def test_an_honoured_deny_is_recorded_on_its_own_delivery() -> None:
    store = a_store()
    await write(frame("frame_tool_result"), store, decision=BLOCKED, webhook_id="msg_9")
    stored = document(store, "deny:msg_9")
    assert stored["event"]["stop_reason"] == "guardrail_intervened"
    assert (stored["verdict"], stored["composed_verdict"]) == ("deny", "deny")
    assert not any(a.startswith("tail:") for a in addresses(store))


async def test_a_shadow_deny_is_an_ordinary_invocation() -> None:
    """The request ran, so a successor frame will arrive and settle this
    record normally. Nothing here may claim a block happened."""
    store = a_store()
    await write(frame("frame_tool_result"), store, decision=SHADOW_DENY, webhook_id="msg_9")
    written = addresses(store)
    assert not any(a.startswith("deny:") for a in written)
    assert any(a.startswith("tail:") for a in written)
    stored = document(store, ADDRESS)
    assert (stored["verdict"], stored["composed_verdict"]) == ("allow", "deny")


async def test_a_record_that_needs_nothing_is_pushed_by_the_frame_that_wrote_it() -> None:
    store = a_store()
    sink = await write(frame("frame_tool_result"), store)
    assert ADDRESS in sink.request_ids
    assert await store.seen(ADDRESS) is Seen.TOMBSTONED


async def test_a_tail_is_never_pushed_on_arrival() -> None:
    """A tail has no expectations, so it is ready the instant it is written
    — and it is the one record that must still wait. Pushing it would emit
    every round twice: once as a tail and once as the previous-run record
    the next frame writes."""
    store = a_store()
    f = frame("frame_tool_result")
    sink = await write(f, store)
    key = tail_address(list(f.messages), f.session_id)
    assert await store.seen(key) is Seen.LIVE
    assert sink.request_ids == [ADDRESS]
```

with `tests/test_write_from_frame.yaml`:

```yaml
# The trailing run holds a tool_use, so both sources can address it. Nothing
# in the attributed round is an upload, so it waits for nothing and is
# pushed on the spot.
id: a_joinable_run_is_addressed_on_its_tool_use_id
fixture: frame_tool_result
append: []
decision: allow
compliance: true
expected: {record: toolu_01Dqhr2d1w2UCUqbXhCSGutC, tail: true}
---
# No previous run to name. Its fresh round is not lost: that is the tail.
id: a_first_turn_writes_only_a_tail
fixture: frame_first_turn
append: []
decision: allow
compliance: true
expected: {record: null, tail: true}
---
id: an_unjoinable_run_falls_back_to_the_delivery_id
fixture: frame_after_shadow_deny
append: []
decision: allow
compliance: true
expected: {record: "hook:msg_1", tail: true}
---
# The attachments are in the attributed round, the run that consumed them
# is joinable, and a reader exists: all three, so the digests are awaited.
id: an_attachment_bearing_joinable_round_waits_for_digests
fixture: frame_attachment
append:
  - {role: assistant, content: [{type: tool_use, id: toolu_09, tool_name: Read, input: {}}]}
  - {role: user, content: [{type: tool_result, tool_use_id: toolu_09, content: ok}]}
decision: allow
compliance: true
expected: {record: toolu_09, tail: true, awaiting: [file_digests]}
---
# The same round with no reader configured. Seeding here would make the
# record wait out the full JOIN_WAIT and then flush exactly what it held.
id: without_compliance_an_attachment_round_waits_for_nothing
fixture: frame_attachment
append:
  - {role: assistant, content: [{type: tool_use, id: toolu_09, tool_name: Read, input: {}}]}
  - {role: user, content: [{type: tool_result, tool_use_id: toolu_09, content: ok}]}
decision: allow
compliance: false
expected: {record: toolu_09, tail: true}
---
# An unjoinable run has no address a reader could look it up by, so its
# attachment digests can never arrive however many readers are configured.
id: an_unjoinable_attachment_round_waits_for_nothing
fixture: frame_attachment
append:
  - {role: assistant, content: [{type: text, text: "that is a PDF about ferries"}]}
  - {role: user, content: [{type: text, text: thanks}]}
decision: allow
compliance: true
expected: {record: "hook:msg_1", tail: true}
---
# The attachment is in the *fresh* round, which belongs to the tail. The
# record being emitted names a run that never saw the file — here there is
# no previous run at all, so there is no record to stamp it on.
id: a_fresh_attachment_does_not_make_the_record_wait
fixture: frame_attachment
append: []
decision: allow
compliance: true
expected: {record: null, tail: true}
---
# An honoured deny produces no response and so no successor frame: nothing
# would ever supersede a tail, so the fresh round is recorded as the denial.
id: an_honoured_deny_replaces_the_tail
fixture: frame_tool_result
append: []
decision: blocked
compliance: true
expected: {record: toolu_01Dqhr2d1w2UCUqbXhCSGutC, tail: false, deny: true}
---
# Shadow mode leaves the correctness path entirely.
id: a_shadow_deny_writes_an_ordinary_tail
fixture: frame_tool_result
append: []
decision: shadow_deny
compliance: true
expected: {record: toolu_01Dqhr2d1w2UCUqbXhCSGutC, tail: true}
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_pending.py -v`. Expected: collection ERROR, `ImportError: cannot import name 'write_from_frame' from 'slashid_anthropic_forwarder.pending'`.

- [ ] **Step 3: Implement** — the imports `pending.py` gains:

```python
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicRequestMessage,
)
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant

from .address import deny_address, hook_address, joinable_address, tail_address
from .hook.checks import Decision
from .hook.envelope import partial_event
from .hook.frame import PromptFrame, split_transcript
from .record import FILE_DIGESTS, HOOK, open_fields
```

and the three functions:

```python
# An empty assistant message, appended so that ``partial_event`` attributes
# the *fresh* round instead of the one before it. It answers nothing, and
# whatever the builder derives from it is cleared below.
_NO_ANSWER_YET = AnthropicRequestMessage(role="assistant", content=[])


def _has_attachment(messages: list[AnthropicRequestMessage]) -> bool:
    """Does the last round of ``messages`` carry an upload?

    Blocks, not ``accessed_files``: an image attachment has no extracted
    text and so yields no entry, yet its stored bytes are exactly what a
    reader can digest and a frame cannot.
    """
    return any(
        isinstance(block, AnthropicAttachmentBlock)
        for message in after_last_assistant(messages)
        for block in message.content
    )


async def _unanswered_round(
    frame: PromptFrame, *, webhook_id: str, signed_at: int, config: Config
) -> AIInvocationObservedV1 | None:
    """The event for the round nothing has answered yet.

    ``partial_event`` names the run *before* the trailing one, so it is
    handed the transcript with one empty assistant message appended: the
    whole frame becomes its ``before`` and the fresh round is the round
    attributed. That appended run answers nothing, so ``output`` and
    ``stop_reason`` are cleared — this record is input-only until a
    successor frame or a reader says otherwise. Its wire ``request_id`` is
    the delivery id, per the design's field mapping, which is not its
    address: a tail is filed under a digest of the transcript so its
    successor can find it.
    """
    whole = frame.model_copy(update={"messages": [*frame.messages, _NO_ANSWER_YET]})
    event = await partial_event(whole, request_id=webhook_id, signed_at=signed_at, config=config)
    if event is None:
        return None
    return event.model_copy(update={"output": None, "stop_reason": None})


async def write_from_frame(
    frame: PromptFrame,
    *,
    decision: Decision,
    webhook_id: str,
    signed_at: int,
    store: PendingStore,
    config: Config,
    client: httpx.AsyncClient,
) -> None:
    """Everything one delivery writes. Called from a background task, after
    the verdict has already gone back to Anthropic."""
    split = split_transcript(frame)
    verdicts = {
        "verdict": decision.answered.action,
        "composed_verdict": decision.composed.action,
    }

    # 1. The previous run. Emit-previous: this frame carries that run's
    #    input and its output both, so the record is complete on arrival
    #    unless a reader owes it attachment digests.
    anchor = joinable_address(split.assistant_run)
    address = anchor or hook_address(webhook_id)
    event = await partial_event(frame, request_id=address, signed_at=signed_at, config=config)
    if event is not None:
        awaiting = (
            (FILE_DIGESTS,)
            if anchor and config.compliance_enabled and _has_attachment(split.before)
            else ()
        )
        fields = open_fields(event, webhook_id=webhook_id, contributed=HOOK, **verdicts)
        outcome = await store.upsert(address, fields, awaiting)
        await push_if_ready(address, outcome, store=store, config=config, client=client)

    # 2. The predecessor's tail. Dropping this frame's trailing run and the
    #    round after it reconstructs the previous delivery's transcript
    #    exactly, so no per-session pointer is needed — which is just as
    #    well, since one session_id covers a hundred sub-conversations.
    #    Retiring an address that was never written is not a mistake: it
    #    leaves a tombstone that makes an out-of-order predecessor's own
    #    upsert a no-op.
    if split.before:
        await store.retire(tail_address(split.before, frame.session_id), Retirement.SUPERSEDED)

    # 3. This frame's own fresh round. Written on every frame: without it a
    #    session's last round has no successor to report it, and the reader
    #    never emits unjoinable runs, so nothing else ever would.
    fresh = await _unanswered_round(
        frame, webhook_id=webhook_id, signed_at=signed_at, config=config
    )
    if fresh is None:
        return
    if not decision.blocked:
        # A shadow-mode deny lands here too: the request ran, a successor
        # frame will arrive, and this tail is discarded by it. Note the
        # missing ``push_if_ready``: a tail has no expectations, so it is
        # ready at once and is the one record that must still wait.
        # Pushing it would emit every round twice — once as a tail, once
        # as the previous-run record the next frame writes.
        await store.upsert(
            tail_address(list(frame.messages), frame.session_id),
            open_fields(fresh, webhook_id=webhook_id, contributed=HOOK, **verdicts),
            (),
        )
        return
    # An honoured deny produces no response and so no successor frame:
    # nothing will ever supersede this. ``guardrail_intervened`` comes from
    # the verdict that actually went back and is stamped now — a flush an
    # hour later could not tell what shadow mode was set to at this moment.
    denial = fresh.model_copy(update={"stop_reason": "guardrail_intervened"})
    key = deny_address(webhook_id)
    outcome = await store.upsert(
        key, open_fields(denial, webhook_id=webhook_id, contributed=HOOK, **verdicts), ()
    )
    await push_if_ready(key, outcome, store=store, config=config, client=client)
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_pending.py -v && uv run ty check`. Expected: `24 passed` (7 from 6.3, 9 yaml cases, 8 new plain), ruff and ty clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/pending.py \
        anthropic/tests/test_pending.py anthropic/tests/test_write_from_frame.yaml
git commit -m "feat(anthropic): write the previous run, the tail, and honoured denials"
```

### Task 6.5: `main.py` — the verdict, the background write, and `/tick`

The handler keeps everything it already does — the body cap, the signature gate, the capture task, allow on anything it cannot parse — and gains two things. It composes a real verdict through Chunk 4's `decide`, and it schedules `write_from_frame` as a background task whose outcome cannot reach the response. That ordering is rule 1, not an optimization: a non-200 is a *webhook failure* that hands control to the organization's fail-open/fail-closed setting, and sustained failures trip Anthropic's circuit breaker, so a Firestore outage must cost events and never verdicts.

`POST /tick` is the other half. Cloud Scheduler posts to it with an OIDC token and none of the `webhook-*` headers, so the signature gate must not run there or every tick would 401. Two details are worth stating plainly. **Route order matters**: Starlette matches in declaration order and `POST /{path:path}` is a catch-all, so a `/tick` declared after it would never be reached. And **a customer whose webhook URL path ends in `/tick` is a genuine collision** — Anthropic posts to whatever URL the admin configured and there is no suffix to reserve. A request carrying `webhook-id` is therefore handled as the delivery it is, which turns a silent swallowing into a working (if ill-advised) configuration; the README says not to do it anyway.

`config-test` frames and frames of unknown top-level `type` return allow before any check and write no record. They carry no invocation, and a pending record for one would have no successor frame — the flush would later push a console connection test as a real invocation against a real user. Returning early also means a 1.86 MB unknown-type frame is not hashed for a check that would bypass anyway.

Finally, `app()` builds the store, which Chunk 5 left to this chunk because nothing in `store.py` reads `Config`. `create_app` still takes one by injection, so every existing test keeps working untouched.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/main.py` (rewrite)
- Test: `anthropic/tests/test_main.py`

- [ ] **Step 1: Write the failing tests** — extend `_client` to take a store and a client, then append. These reuse `test_pending.py`'s helpers rather than introducing a second test double: the store under them is the real `FirestorePendingStore` over Chunk 5's fake client.

```python
def _client(
    config: Config,
    capture: MemoryCapture | None = None,
    store: FirestorePendingStore | None = None,
    sink: Sink | None = None,
) -> httpx.AsyncClient:
    app = create_app(config, capture=capture, store=store, client=(sink or Sink()).client())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


TOOL_FRAME = json.loads((pathlib.Path(__file__).parent / "fixtures" / "frame_tool_result.json").read_text())


async def test_a_delivery_writes_its_records(sign: Signer) -> None:
    body = json.dumps(TOOL_FRAME).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert any(a.startswith("tail:") for a in addresses(store))


async def test_a_store_failure_never_reaches_the_verdict(sign: Signer) -> None:
    """Rule 1: a non-200 is a webhook failure, and enough of them disable
    enforcement for the whole organization."""

    class Broken(FirestorePendingStore):
        async def upsert(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("firestore unreachable")

    store = Broken(client=FakeFirestore(), collection="c", join_wait=timedelta(hours=1))
    body = json.dumps(TOOL_FRAME).encode()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_the_record_is_written_after_the_response_is_sent(sign: Signer) -> None:
    """Not merely isolated from the verdict — it does not delay it."""
    order: list[str] = []

    class Noted(FirestorePendingStore):
        async def upsert(self, *args: Any, **kwargs: Any) -> Any:
            order.append("store")
            return await super().upsert(*args, **kwargs)

    store = Noted(client=FakeFirestore(), collection="c", join_wait=timedelta(hours=1))
    body = json.dumps(TOOL_FRAME).encode()
    headers = sign(body, "msg_1")
    app = create_app(_config(), store=store, client=Sink().client())

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            order.append("response")

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/",
            "raw_path": b"/",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 1),
            "server": ("t", 80),
        },
        receive,
        send,
    )
    assert order[0] == "response" and "store" in order


async def test_a_config_test_frame_writes_nothing(sign: Signer) -> None:
    body = json.dumps({**TOOL_FRAME, "source": {"application": "config-test"}}).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert addresses(store) == set()


async def test_an_unknown_type_writes_nothing(sign: Signer) -> None:
    body = json.dumps({**TOOL_FRAME, "type": "response"}).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert addresses(store) == set()


async def test_the_tick_needs_no_signature() -> None:
    async with _client(_config(), store=a_store()) as c:
        r = await c.post("/tick")
    assert r.status_code == 200
    assert r.json() == {"flushed": 0}


async def test_the_tick_flushes_records_past_their_deadline() -> None:
    store = a_store(join_wait=timedelta(seconds=-1))  # born already due
    await seed(store)
    sink = Sink()
    async with _client(_config(), store=store, sink=sink) as c:
        r = await c.post("/tick")
    assert r.json() == {"flushed": 1}
    assert sink.request_ids == [ADDRESS]


async def test_a_delivery_posted_to_slash_tick_is_still_a_delivery(sign: Signer) -> None:
    """A customer whose webhook URL ends in /tick would otherwise have every
    frame swallowed by the scheduler route."""
    body = json.dumps(TOOL_FRAME).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/tick", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert addresses(store) != set()
```

importing `pathlib`, `timedelta`, `FirestorePendingStore`, `FakeFirestore`, and `ADDRESS`/`Sink`/`a_store`/`addresses`/`seed` from `tests.test_pending`. `a_store` needs `join_wait` to be overridable — it already is, since it forwards `**over` to the constructor; make sure it does not also pass `join_wait` positionally.

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_main.py -v`. Expected: `TypeError: create_app() got an unexpected keyword argument 'store'` on the new cases; the ten existing ones still pass.

- [ ] **Step 3: Implement** — rewrite `main.py`:

```python
"""FastAPI entrypoint: the hook on any path, the flush on /tick.

Owns rule 1 of the design: nothing after the verdict is decided — the
capture, the pending write, the push — may change the response. A non-200
is a webhook failure, which hands control to the organization's
fail-open/fail-closed setting, and sustained failures trip Anthropic's
circuit breaker and disable enforcement entirely.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .config import Config, load_config
from .hook.capture import Capture, GcsCapture
from .hook.checks import ALLOW, Decision
from .hook.envelope import accessed_files_for
from .hook.frame import PromptFrame
from .hook.signature import verify
from .hook.verdict import decide
from .pending import flush_due, write_from_frame
from .store import FirestorePendingStore, PendingStore

log = logging.getLogger(__name__)


async def _capture_safely(capture: Capture, request_id: str, headers: dict, body: bytes) -> None:
    try:
        await capture.store(request_id, headers, body)
    except Exception:
        log.exception("capture failed for %s", request_id)


async def _write_safely(**kwargs: Any) -> None:
    """Rule 1's other half: the write runs after the response is sent, and
    its failure is a log line, never a status code."""
    try:
        await write_from_frame(**kwargs)
    except Exception:
        log.exception("pending write failed for %s", kwargs.get("webhook_id"))


def _parse(body: bytes) -> PromptFrame | None:
    try:
        return PromptFrame.model_validate(json.loads(body))
    except Exception:
        return None


def _signed_at(headers: dict[str, str]) -> int:
    """The attested webhook-timestamp. Falling back to now keeps an
    unsigned-mode deployment from losing the event over a missing header."""
    try:
        return int(headers["webhook-timestamp"])
    except (KeyError, ValueError):
        return int(time.time())


def pending_store(config: Config) -> PendingStore:
    """Build the store from configuration. It lives here rather than in
    ``store.py`` because nothing below the port reads ``Config``."""
    from google.cloud import firestore

    return FirestorePendingStore(
        client=firestore.AsyncClient(
            project=config.gcp_project_id, database=config.firestore_database
        ),
        collection=config.pending_collection,
        join_wait=timedelta(seconds=config.join_wait_seconds),
    )


def create_app(
    config: Config,
    *,
    capture: Capture | None = None,
    store: PendingStore | None = None,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    if capture is None and config.capture_bucket:
        capture = GcsCapture(config.capture_bucket)
    held: dict[str, httpx.AsyncClient | None] = {"client": client}

    def http() -> httpx.AsyncClient:
        # Lazily built and shared: ASGITransport does not run lifespan
        # events, so tests inject their own rather than relying on startup.
        if held["client"] is None:
            held["client"] = httpx.AsyncClient(timeout=config.request_timeout_seconds)
        return held["client"]

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        if client is None and held["client"] is not None:
            await held["client"].aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def handle_frame(request: Request, background: BackgroundTasks) -> Response:
        body = await request.body()
        if len(body) > config.max_body_bytes:
            return Response(status_code=413)
        headers = {k: v for k, v in request.headers.items()}
        if not verify(config.signing_secrets, headers, body) and not config.hook_allow_unsigned:
            return Response(status_code=401)
        webhook_id = headers.get("webhook-id", "")

        if capture is not None:
            background.add_task(_capture_safely, capture, webhook_id, headers, body)

        frame = _parse(body)
        if frame is None:
            # The frame is inspected, not validated for the verdict: a
            # shape we cannot parse is answered, not rejected.
            log.warning("frame %s did not parse; allowing", webhook_id)
            return JSONResponse(ALLOW.to_wire(), background=background)
        if frame.type != "prompt" or frame.is_connection_test():
            # No invocation, so no record: a pending one would have no
            # successor frame, and the flush would later push a console
            # connection test as a real invocation against a real user.
            log.info("frame %s is %r/%s; allowing", webhook_id, frame.type, frame.source.application)
            return JSONResponse(ALLOW.to_wire(), background=background)

        files = await accessed_files_for(frame.messages, config=config)
        decision: Decision = await decide(
            frame, raw_body=body, headers=headers, files=files, config=config, client=http()
        )
        if store is not None:
            background.add_task(
                _write_safely,
                frame=frame,
                decision=decision,
                webhook_id=webhook_id,
                signed_at=_signed_at(headers),
                store=store,
                config=config,
                client=http(),
            )
        return JSONResponse(decision.answered.to_wire(), background=background)

    # Declared first: POST /{path:path} is a catch-all and Starlette matches
    # in declaration order, so a /tick declared after it is unreachable.
    @app.post("/tick")
    async def tick(request: Request, background: BackgroundTasks) -> Response:
        # Cloud Scheduler posts here with an OIDC token and no webhook
        # headers, so the signature gate must not run — it would 401 every
        # tick. A customer whose configured webhook URL happens to end in
        # /tick is a real collision: Anthropic posts to whatever path the
        # admin set and no suffix is reserved. A request carrying
        # webhook-id is therefore the delivery it claims to be.
        if "webhook-id" in request.headers:
            return await handle_frame(request, background)
        if store is None:
            return JSONResponse({"flushed": 0})
        flushed = await flush_due(store, config=config, client=http())
        return JSONResponse({"flushed": flushed})

    @app.post("/{path:path}")
    async def hook(request: Request, background: BackgroundTasks) -> Response:
        return await handle_frame(request, background)

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
    config = load_config()
    return create_app(config, store=pending_store(config))
```

`_reference_id` and the inline deny-marker branch are gone: both moved into `hook/verdict.py` in Chunk 4, and the existing marker test still passes because `decide` applies the marker under the same shadow-mode rule. The readers join `tick` in the compliance chunk, **ahead of** the flush — a reader's `complete` can make a record ready, and it should push on this tick rather than the next.

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest -v && uv run ty check`. Expected: the ten pre-existing `test_main.py` cases plus 8, the whole subproject green, ruff and ty clean.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/main.py anthropic/tests/test_main.py
git commit -m "feat(anthropic): wire the verdict, the pending write and a tick route"
```

---

## Chunk 7: The compliance readers

The pull half of the component. Four modules under `anthropic/src/slashid_anthropic_forwarder/compliance/`, plus the reader knobs, the checkpoint type promoted out of `vertex/`, one small addition to `record.py`, and the wiring that puts all of it on `POST /tick`. `client.py` talks to the three feeds — activities, chats, local sessions — which **do not share a query vocabulary**; encoding that correctly is the point of the module, because the one way to get it wrong (resuming the activity feed without `order=asc`) fails silently and looks healthy. `checkpoint.py` keeps three independent cursors. `denials.py` is Reader A: it polls `inference_hooks_request_denied`, filters the rest of the feed out, and looks its record up by `deny_address(activity["request_id"])`, because that `request_id` **is** the `webhook-id` Chunk 6 filed the denial under. `responses.py` is Reader B and `attachments.py` is its enrichment tier.

**Everything here is built on Chunks 5 and 6, and uses their names rather than inventing parallel ones.**

| From | Used for |
| --- | --- |
| `address.joinable_address(run) -> str \| None` | the only key both sources compute. **`None` is the unjoinable signal** — there is no reader-side digest fallback, because a transcript-prefix digest is exactly the key the 200/302/**zero**-overlap measurement killed |
| `address.deny_address(webhook_id)` | Reader A's lookup. One definition, imported, never re-derived |
| `record.event_fields(event)` / `record.open_fields(...)` | the document shape. The event is nested under `"event"`, which is where `from_document` reads it and where the 1 MiB bound is applied |
| `record.COMPLIANCE`, `record.Append` | `contributed`, without which `to_event` labels every reader event `anthropic-inference-hook` and an enriched record never becomes `anthropic-joined` |
| `record.FILE_DIGESTS` | the one expectation a record can hold, and the key the digests arrive under |
| `store.PendingStore`, `Seen`, `Outcome` | `seen` first, then `upsert` or `complete` |
| `pending.push_if_ready(address, outcome, ...)` | claim, push, retire — the same arbitration the frame path uses. A standalone emission is an `upsert` that leaves the record ready, so this pushes it and the `retire(PUSHED)` inside leaves the tombstone that stops the next tick re-emitting it |

**Two walks, not one.** The two conversation feeds are different shapes, and one function cannot read both:

| | local session | chat |
| --- | --- | --- |
| produced turn | assistant message carrying `model`, with no `provenance` | every assistant message; the transcript is the canonical store, not a replay |
| `model` | on the message | on the **chat object** — no message carries one |
| `provenance` | `{"type": …}` or absent | never present |
| tool results | in the next **user** message, as the frame shows them | **inside the assistant message**, beside the `tool_use` that asked |

The last row is the trap. `AnthropicMessage` is the response-side union and does not admit `tool_result`, so handing a chat assistant message's blocks to it straight raises a `ValidationError` — inside a tick, that takes down every reader behind it. The chat walk filters the answer to response-side blocks; the transcript half is unaffected, since `AnthropicRequestMessage` admits `tool_result` in either role.

**The fixtures already exist and are committed**, recorded from the live tenant by `anthropic/scripts/record_compliance_fixtures.py` and guarded by `anthropic/tests/test_fixtures_scrubbed.py`, which re-checks every byte including what hides inside base64. They are the corpus every test below runs on, and they pin one more thing: `tests/fixtures/paired/` holds the frames captured **alongside** those transcripts, so the claim that a run's address is byte-identical across the two sources is measured here rather than asserted.

---

### Task 7.1: the reader knobs

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/config.py`
- Test: `anthropic/tests/test_config.py`

Task 6.1 already added `compliance_key` and `compliance_enabled` — the `awaiting` seeding rule needed them on the request path. This adds the five knobs the readers themselves read, plus the checkpoint collection, and nothing else: the capability rule (`hook_enabled`, at least one credential) stays in Task 8.2 where the design's two conflicts are settled together.

**Overlap to settle when Chunk 8 is written:** Task 8.1 lists twelve fields, and six of them land here — `organization_uuid`, `poll_lag_seconds`, `max_sessions_per_tick`, `attachment_hashing`, `max_attachment_fetch_bytes`, `checkpoint_collection`. Task 8.1 keeps `tick_interval_seconds`, the `gcp_project_id`-is-required change and the `mode="before"` validator that turns `""` into `None` — and that validator must still name `compliance_key` and `organization_uuid`, which is why it stays there rather than moving here: Terraform sets every variable it manages, and `SLASHID_COMPLIANCE_KEY=""` would otherwise switch the readers on with an empty key.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`, using that module's own `_config` builder:

```python
def test_the_reader_knobs_have_the_designed_defaults() -> None:
    config = _config()
    assert config.organization_uuid is None
    assert config.poll_lag_seconds == 120
    assert config.max_sessions_per_tick == 200
    assert config.attachment_hashing == "md5"
    assert config.max_attachment_fetch_bytes == 10 * 1024 * 1024
    assert config.checkpoint_collection == "anthropic_checkpoints"


def test_attachment_hashing_must_be_md5_or_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_ATTACHMENT_HASHING="sha256")
    with pytest.raises(ValidationError):
        Config()
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_config.py -v`. Expected: two failures — `AttributeError: 'Config' object has no attribute 'organization_uuid'`, and `Failed: DID NOT RAISE <class 'pydantic_core.ValidationError'>` on the second, because an unmodelled env var is ignored (`extra="ignore"`) rather than rejected.

- [ ] **Step 3: Implement** — add to `Config`, after the compliance key Task 6.1 introduced:

```python
    # The key can read every linked organization while the hook's tenant
    # binding is per organization, so the readers filter to this one. It
    # equals the frame's `tenant_id`. Not a query parameter: both
    # listings reject `organization_uuid`, so the filter runs over the
    # rows a listing returns.
    organization_uuid: str | None = None
    # How far behind now the `updated_at.gte` bound sits, and the initial
    # watermark on a cold start — never a full backfill.
    poll_lag_seconds: int = 120
    # Bounds one tick against the 600 rpm shared with the sync adapter.
    # The local-session listing cannot be ordered, so a tick that hits
    # this cap leaves the *oldest* sessions untouched and must not
    # advance its watermark.
    max_sessions_per_tick: int = 200
    # `md5` takes the digest the file listing already carries and makes
    # no extra request. `full` downloads the stored bytes for sha1 and
    # sha256, which OneDrive, SharePoint and Drive resources need.
    attachment_hashing: Literal["md5", "full"] = "md5"
    # Under `full`, the largest attachment worth downloading. Decided
    # from the listing's `size_bytes` *before* any fetch: an oversized
    # file is never started, and falls back to the listing's md5 rather
    # than to no digest. A ranged read yields a snippet, never a digest.
    max_attachment_fetch_bytes: int = 10 * 1024 * 1024
    # One document per feed, in its own collection: a watermark is a
    # different lifetime from a pending record, and the pending
    # collection carries a TTL policy that would delete these.
    checkpoint_collection: str = "anthropic_checkpoints"
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_config.py -v && uv run ruff check . && uv run ty check`. Expected: the module's existing cases plus 2.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/config.py anthropic/tests/test_config.py
git commit -m "feat(anthropic): the compliance reader knobs"
```

### Task 7.2: the recorded corpus, and one transport that serves it

**Files:**
- Create: `anthropic/tests/compliance_fixtures.py`
- Test: `anthropic/tests/test_compliance_fixtures.py`
- Read only: `anthropic/tests/fixtures/compliance/*.json`, `anthropic/scripts/record_compliance_fixtures.py`, `anthropic/tests/test_fixtures_scrubbed.py`

The corpus is committed; this task is the double that serves it, written **before** any reader so no reader is ever tested against a hand-built dict. It lives in its own module, like Chunk 5's `tests/fake_firestore.py`, because three test modules import it.

Two properties matter, and the second is the one a naive double gets wrong.

**Route by path, never by call order.** One reader pass touches four endpoints in an order that depends on the data; a double that replays a list positionally serves a chat listing to a session request. Every fixture declares the path it answers in its own `request.path`, so the routing table builds itself from the corpus and cannot drift from it.

**Never repeat a body.** The earlier draft of this double answered every request with the last recorded body, which turns a pager into an infinite loop: `iter_activities` follows `last_id` while `has_more` is true, so the test hangs instead of failing. Serving each path its own recorded body once — and 404 for an unknown path — makes a wrong request a visible failure. As it happens every recorded listing has `has_more: false` and `next_page: null`, so the pagers terminate on the first page; the one test that needs a second page builds a synthetic pair inline and says so.

What the corpus holds:

| Fixture | What it is |
| --- | --- |
| `sessions_list.json` | `GET /apps/sessions/local?limit=30` → `{data, next_page}`, six sessions. Item keys: `type, id, organization_uuid, workspace_id, user, product_surface, created_at, updated_at, truncated`. **`user` is here** — no message carries one |
| `session_messages_1..6.json` | `GET /apps/sessions/local/{clls_id}/messages?limit=1000&tool_result_max_bytes=-1&tool_use_input_max_bytes=-1` → `{session, data, next_page}`. Message keys are exactly `type, id, role, created_at, provenance, model, content` |
| `chats_list.json` | `GET /apps/chats?limit=100` → `{data, has_more, first_id, last_id}`. The chat item carries `model` and `user` |
| `chat_messages_1..3.json` | `GET /apps/chats/{claude_chat_id}/messages` → the **chat object**, turns under `chat_messages`; message keys `id, role, created_at, content, files, generated_files, artifacts` — no `model`, no `provenance` |
| `activities.json` | `GET /activities?limit=1000&order=asc` → 26 rows over ten types, exactly one `inference_hooks_request_denied` |
| `filters_rejected.json` | a `cases` list: the four rejections, plus `GET /organizations` answering 200 |
| `organizations_me.json` | `GET /organizations/me` answering **404** — the `/me` suffix does not exist under `/v1/compliance`; `/organizations` is the one that answers, and it is in `filters_rejected.json` |

Three fixtures earn their place by being awkward. `session_messages_3.json` is a **tool-free run** — the unjoinable case, and the one Reader B must leave alone. `chat_messages_2.json` has five assistant messages with `tool_result` blocks inline and two artifacts. `chat_messages_1.json` carries the three-entry `files[]` — `guiaSADT.pdf` (59430 B), a JPEG (72878 B) and `maria.txt` (27 B, md5 `9ae4c5f2489fadc563c6f747d6298fe4`), which is **byte-identical to the extracted text in `frame_attachment.json`** and is what makes the `full`-tier test a real digest comparison rather than a mock agreeing with itself.

- [ ] **Step 1: Write the helper** — `anthropic/tests/compliance_fixtures.py`. It is test infrastructure, not a test:

```python
"""The recorded compliance corpus, and one transport that serves it.

Recorded from the live tenant by ``scripts/record_compliance_fixtures.py``
and scrubbed; ``test_fixtures_scrubbed.py`` re-checks every byte, base64
included. Each file is ``{request: {method, path, params}, status, body}``,
except ``filters_rejected.json``, which holds a ``cases`` list of the same
shape.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "compliance"
PAIRED = Path(__file__).parent / "fixtures" / "paired"

# The stored bytes of the smallest attachment in the corpus. Its md5 is the
# one `chat_messages_1.json` lists, and it is byte-identical to the text
# `frame_attachment.json` carries — which is the measured claim that plain
# text crosses the two surfaces unchanged.
MARIA_ID = "claude_file_01MpfHfLBGDPcEBwPQvWZMbY"
MARIA_BYTES = b"Maria tinha um carneirinho\n"


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def body(name: str) -> dict[str, Any]:
    return recorded(name)["body"]


def cases(name: str = "filters_rejected.json") -> list[dict[str, Any]]:
    return recorded(name)["cases"]


def _routes() -> dict[str, str]:
    """Path → fixture, built from the corpus so it cannot drift from it."""
    table: dict[str, str] = {}
    for path in sorted(FIXTURES.iterdir()):
        payload = json.loads(path.read_text())
        request = payload.get("request")
        if isinstance(request, dict) and request.get("path"):
            table[request["path"]] = path.name
    return table


ROUTES = _routes()


def transport(
    *, files: Mapping[str, bytes] | None = None
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A client serving the corpus by path, and the requests it saw.

    By path and never by call order: one reader pass touches four
    endpoints in a data-dependent order. An unrouted path is a 404, which
    surfaces a wrong request as a failure instead of as a plausible body.
    """
    seen: list[httpx.Request] = []
    bodies = {MARIA_ID: MARIA_BYTES, **(files or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix("/v1/compliance")
        if "/files/" in path:
            file_id = path.split("/files/")[1].split("/")[0]
            if file_id not in bodies:
                return httpx.Response(404, json={"type": "error"})
            return httpx.Response(200, content=bodies[file_id])
        name = ROUTES.get(path)
        if name is None:
            return httpx.Response(404, json={"type": "error", "path": path})
        entry = recorded(name)
        return httpx.Response(entry["status"], json=entry["body"])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen
```

- [ ] **Step 2: Write its tests** — `anthropic/tests/test_compliance_fixtures.py`:

```python
"""The double itself, because three modules trust it."""

from __future__ import annotations

import hashlib
import json

from tests.compliance_fixtures import (
    MARIA_BYTES,
    MARIA_ID,
    PAIRED,
    ROUTES,
    body,
    cases,
    transport,
)


def test_every_listing_endpoint_is_routed() -> None:
    assert {"/apps/sessions/local", "/apps/chats", "/activities"} <= set(ROUTES)


async def test_a_path_is_served_once_and_an_unknown_path_404s() -> None:
    client, seen = transport()
    async with client:
        first = await client.get("https://api.anthropic.com/v1/compliance/apps/chats")
        missing = await client.get("https://api.anthropic.com/v1/compliance/nope")
    assert first.json() == body("chats_list.json")
    assert missing.status_code == 404
    assert len(seen) == 2


async def test_the_file_body_matches_the_listed_md5() -> None:
    # Not a mock agreeing with itself: the listing's md5 was recorded from
    # the tenant and these bytes are the frame's extracted text.
    listed = next(
        entry
        for message in body("chat_messages_1.json")["chat_messages"]
        for entry in (message.get("files") or [])
        if entry["id"] == MARIA_ID
    )
    assert hashlib.md5(MARIA_BYTES).hexdigest() == listed["md5"]
    assert len(MARIA_BYTES) == listed["size_bytes"]


def test_the_paired_frames_cover_the_recorded_sessions() -> None:
    # tests/fixtures/paired/session_N_frame_M.json was captured alongside
    # session_messages_N.json. Task 7.9 measures the address across them.
    sessions = {json.loads(p.read_text())["session_id"] for p in PAIRED.glob("*.json")}
    assert len(sessions) == 6


def test_the_recorded_rejections_are_the_four_the_client_encodes() -> None:
    rejected = [c for c in cases() if c["status"] >= 400]
    params = " ".join(c["request"]["params"] for c in rejected)
    assert "updated_at.gte" in params  # chats, without order_by
    assert "order=asc" in params  # local sessions
    assert "order_by=updated_at" in params  # local sessions, the other one
    assert "created_at%5Bgte%5D" in params  # the bracketed form
    assert any(c["status"] == 200 and c["request"]["path"] == "/organizations" for c in cases())
```

- [ ] **Step 3: Run** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_compliance_fixtures.py tests/test_fixtures_scrubbed.py -v && uv run ruff check .`. Expected: 5 passed here, and the scrub guard green — it already is, and this task must not change it.

- [ ] **Step 4: Commit**

```bash
git add anthropic/tests/compliance_fixtures.py anthropic/tests/test_compliance_fixtures.py
git commit -m "test(anthropic): route the recorded compliance corpus by path"
```

### Task 7.3: `client.py` — three feeds, three query vocabularies

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/compliance/__init__.py` (empty)
- Create: `anthropic/src/slashid_anthropic_forwarder/compliance/client.py`
- Test: `anthropic/tests/test_client.py`, `anthropic/tests/test_feed_vocabulary.yaml`

One adapter per feed, not one generic pager. The differences are measured and none is cosmetic:

| Feed | Lower bound | Ordering | Page token |
| --- | --- | --- | --- |
| activities | `created_at.gte` | `order=asc`; the default is `desc` | `last_id`, sent back as `after_id` |
| chats | `updated_at.gte`, **rejected** unless ordered | `order_by=updated_at` — `order_by`, not `order` | `last_id`, sent back as `after_id` |
| local sessions | `updated_at.gte` | **no ordering parameter exists**; both are rejected; newest-first | `next_page` |

The activity one is the dangerous one, because getting it wrong produces no error: the feed answers 200 with full pages, and a reader resuming from its watermark without `order=asc` walks steadily further into the past and never sees a new denial. Local sessions being unorderable is why the sessions adapter returns a `Drain` carrying `complete` rather than an iterator — a truncated drain is a value the caller must handle, so it is in the type.

Three more facts the paths and filters rest on: the session listing is `/apps/sessions/local`, **not** `/apps/local_sessions`; the bound is dotted, `created_at[gte]` is rejected; and `organization_uuid` is not a query parameter on either listing, so the bound-organization filter runs in Python over the rows a listing returns.

The client **does not own its httpx client**: the tick shares one with the push path, and headers are per request. So no `aclose`, no context manager — a double-closed client is a failure mode this module has no reason to invent.

- [ ] **Step 1: Write the failing tests** — `anthropic/tests/test_client.py`:

```python
"""The three feeds, against the recorded corpus."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.compliance.client import (
    ComplianceClient,
    ComplianceError,
    decode_session_id,
    provenance_type,
)
from tests.compliance_fixtures import MARIA_BYTES, MARIA_ID, body, cases, transport

SINCE = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def a_client() -> tuple[ComplianceClient, list[httpx.Request]]:
    client, seen = transport()
    return ComplianceClient(client, api_key="sk-ant-api01-fixture"), seen


@yaml_pytest(filename="test_feed_vocabulary.yaml")
async def test_feed_vocabulary(feed: str, params: dict[str, str]) -> None:
    client, seen = a_client()
    await _drain(client, feed)
    query = dict(seen[0].url.params)
    assert {k: query.get(k) for k in params} == params
    # Nothing a feed rejects may leak in from another feed's vocabulary —
    # `order` on chats and either ordering on sessions are recorded 4xx.
    assert set(query) <= set(params) | {"limit", "after_id", "page"}


async def test_requests_carry_the_key_and_the_version() -> None:
    client, seen = a_client()
    [_ async for _ in client.iter_activities(since=SINCE)]
    assert seen[0].headers["x-api-key"] == "sk-ant-api01-fixture"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"


async def test_activities_stop_when_has_more_is_false() -> None:
    client, seen = a_client()
    rows = [row async for row in client.iter_activities(since=SINCE)]
    assert len(rows) == len(body("activities.json")["data"])
    assert len(seen) == 1


async def test_a_second_page_is_fetched_with_after_id() -> None:
    # No second page was recorded — the tenant's whole window fits one —
    # so this pair is synthetic, and it exists only to pin the cursor
    # parameter the recorded body names (`last_id`) to the one the
    # request sends (`after_id`).
    pages = [
        {"data": [{"id": "a", "type": "x"}], "has_more": True, "last_id": "cursor-1"},
        {"data": [{"id": "b", "type": "x"}], "has_more": False, "last_id": "cursor-2"},
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[min(len(seen) - 1, 1)])

    client = ComplianceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_key="k"
    )
    rows = [row async for row in client.iter_activities(since=SINCE)]
    assert [r["id"] for r in rows] == ["a", "b"]
    assert dict(seen[1].url.params)["after_id"] == "cursor-1"


async def test_local_sessions_drain_is_marked_incomplete_at_the_cap() -> None:
    client, _ = a_client()
    drain = await client.drain_local_sessions(since=SINCE, limit=2)
    assert len(drain.sessions) == 2
    assert drain.complete is False


async def test_a_completed_drain_says_so() -> None:
    client, _ = a_client()
    drain = await client.drain_local_sessions(since=SINCE, limit=500)
    assert len(drain.sessions) == len(body("sessions_list.json")["data"])
    assert drain.complete is True


async def test_decode_session_id_yields_the_frames_session_id() -> None:
    decoded = {decode_session_id(s["id"]) for s in body("sessions_list.json")["data"]}
    # The paired frames carry exactly these as `session_id`; that is the
    # whole join between a captured delivery and a stored transcript.
    assert "00000001-0000-4000-8000-000000000000" in decoded
    assert None not in decoded


def test_decode_session_id_survives_a_missing_pad_and_refuses_junk() -> None:
    assert decode_session_id("clls_not-base64") is None
    assert decode_session_id("sess_01ABC") is None


def test_provenance_is_an_object_not_a_string() -> None:
    assert provenance_type({"provenance": {"type": "client_asserted"}}) == "client_asserted"
    assert provenance_type({"provenance": {"type": "content_unavailable", "reason": "oversize"}}) == (
        "content_unavailable"
    )
    # Unknown values are tolerated by the schema and by us: skipped, never
    # rejected. A bare string was never the shape, and `None` is the shape
    # a produced local-session turn actually has.
    assert provenance_type({"provenance": {"type": "future_kind"}}) == "future_kind"
    assert provenance_type({"provenance": None}) is None
    assert provenance_type({}) is None


async def test_a_chat_transcript_lives_under_chat_messages_not_data() -> None:
    # The chats endpoint answers the chat object, so its turns are under
    # `chat_messages`. Reading `data` yields nothing and says nothing.
    client, _ = a_client()
    chat = body("chats_list.json")["data"][1]
    messages = await client.chat_messages(chat["id"])
    assert messages
    assert messages == body("chat_messages_2.json")["chat_messages"]


async def test_the_chat_object_is_available_for_its_model() -> None:
    # No chat message carries a model; the chat does, and Reader B needs
    # it, so the client hands back both halves.
    client, _ = a_client()
    chat = body("chats_list.json")["data"][1]
    fetched = await client.chat(chat["id"])
    assert fetched["model"] == body("chat_messages_2.json")["model"]


async def test_a_session_transcript_comes_back_whole() -> None:
    client, _ = a_client()
    session = body("sessions_list.json")["data"][0]
    messages = await client.session_messages(session["id"])
    assert len(messages) == len(body("session_messages_1.json")["data"])


async def test_tool_caps_ride_on_a_transcript_request() -> None:
    client, seen = a_client()
    session = body("sessions_list.json")["data"][0]
    await client.session_messages(session["id"], tool_block_bytes=-1)
    query = dict(seen[0].url.params)
    assert query["tool_result_max_bytes"] == "-1"
    assert query["tool_use_input_max_bytes"] == "-1"


async def test_file_content_is_the_whole_body() -> None:
    client, _ = a_client()
    assert await client.file_content(MARIA_ID) == MARIA_BYTES


async def test_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate_limited"})

    client = ComplianceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_key="k"
    )
    with pytest.raises(ComplianceError):
        [_ async for _ in client.iter_activities(since=SINCE)]


def test_the_rejections_the_vocabulary_avoids_were_recorded() -> None:
    assert len([c for c in cases() if c["status"] >= 400]) == 5


async def _drain(client: ComplianceClient, feed: str) -> None:
    if feed == "activities":
        [_ async for _ in client.iter_activities(since=SINCE)]
    elif feed == "chats":
        [_ async for _ in client.iter_chats(since=SINCE)]
    else:
        await client.drain_local_sessions(since=SINCE, limit=10)
```

`anthropic/tests/test_feed_vocabulary.yaml` — the table that pins the three vocabularies apart:

```yaml
# The activity feed defaults to desc. `order=asc` is not a preference: a
# reader that omits it resumes from its watermark and pages backwards
# into the past forever, with 200s and full pages the whole way.
id: activities_ask_ascending_explicitly
feed: activities
params:
  created_at.gte: "2026-09-20T12:00:00+00:00"
  order: asc
---
# `updated_at.gte` is rejected unless the ordering rides with it, and the
# parameter is `order_by`. Sending `order` here is a recorded 4xx.
id: chats_need_order_by_with_the_bound
feed: chats
params:
  updated_at.gte: "2026-09-20T12:00:00+00:00"
  order_by: updated_at
---
# No ordering parameter exists at all — both `order` and `order_by` are
# recorded rejections — so the bound is the only thing this feed takes.
id: local_sessions_take_the_bound_and_nothing_else
feed: local_sessions
params:
  updated_at.gte: "2026-09-20T12:00:00+00:00"
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_client.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.compliance'`.

- [ ] **Step 3: Implement** `compliance/client.py`:

```python
"""The Compliance API — three feeds that do not share a query vocabulary.

Base ``https://api.anthropic.com/v1/compliance``; every request carries
``x-api-key`` and ``anthropic-version: 2023-06-01``.

| Feed           | Lower bound        | Ordering                | Page token   |
| -------------- | ------------------ | ----------------------- | ------------ |
| activities     | ``created_at.gte`` | ``order=asc``           | ``last_id``  |
| chats          | ``updated_at.gte`` | ``order_by=updated_at`` | ``last_id``  |
| local sessions | ``updated_at.gte`` | none exists             | ``next_page``|

Measured against the live API, and none of the three differences is
cosmetic:

* activities default to ``desc``. Resuming from a saved watermark
  without ``order=asc`` walks steadily further into the past and never
  sees a new denial — 200s, full pages, zero new rows, no error.
* chats **reject** ``updated_at.gte`` unless ``order_by=updated_at``
  rides with it, and the parameter is ``order_by``, not ``order``.
* local sessions accept neither ``order`` nor ``order_by`` — both are
  rejected — and answer newest-first. There is no forward stream, so a
  reader drains the whole lagging window each tick and the drain
  reports whether it finished.

Three more facts the paths and the filters rest on. The local session
listing is ``/apps/sessions/local``, **not** ``/apps/local_sessions``.
The bound is dotted — ``created_at[gte]`` is rejected. And
``organization_uuid`` is not a query parameter on either listing, so
filtering to the bound organization happens in Python, over the rows a
listing returns.

The envelopes differ too: the two ordered feeds answer ``{data,
has_more, first_id, last_id}``, the session listing and a session
transcript answer ``{data, next_page, …}``, and a **chat** transcript
answers the chat object itself, whose turns sit under ``chat_messages``
and whose ``model`` no message repeats.

This client does not own its ``httpx.AsyncClient``: the tick shares one
with the push path, and every header here is per request.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.anthropic.com/v1/compliance"
API_VERSION = "2023-06-01"

_ACTIVITIES = "/activities"
_CHATS = "/apps/chats"
# Not ``/apps/local_sessions``: that 404s. The noun order is the other
# way round, and it is the one path here worth pinning in a constant.
_SESSIONS = "/apps/sessions/local"
_FILE_CONTENT = "/apps/chats/files/{file_id}/content"
# Transcript pages, which a listing's limit does not bound.
_MESSAGE_PAGE = 1_000

# Transcript endpoints cap each tool block at this many bytes and flag the
# block ``truncated``; ``-1`` asks for the whole block (~1 MiB ceiling).
TOOL_BLOCK_DEFAULT_BYTES = 10_000
TOOL_BLOCK_FULL = -1

DENIED_ACTIVITY = "inference_hooks_request_denied"
# Our own reads are audited as this, so a poller adds noise to the
# tenant's audit record and Reader A filters the feed by type.
OWN_READ_ACTIVITY = "compliance_api_accessed"

# ``provenance`` is an object — {"type": "client_asserted"} — never a bare
# string, and a fourth value exists: ``content_unavailable``, carrying a
# ``reason`` of not_captured, client_aborted, cmek_key_revoked,
# retention_elapsed or oversize. A produced turn has no provenance at all.
REPLAYED = "client_asserted"
SYNTHETIC = "synthetic_marker"
UNAVAILABLE = "content_unavailable"
NOT_PRODUCED = frozenset({REPLAYED, SYNTHETIC, UNAVAILABLE})


class ComplianceError(Exception):
    """A feed answered something other than 2xx, or unparseable JSON."""


@dataclass(frozen=True)
class Drain:
    """A pass over the unorderable local-sessions listing.

    ``complete`` is False when the cap cut the listing. The listing is
    newest-first, so the untouched tail is the *oldest* — advancing the
    watermark past it would lose those sessions permanently, hardest on
    the busiest tenants and immediately after an outage.
    """

    sessions: list[dict[str, Any]]
    complete: bool


def provenance_type(message: Mapping[str, Any]) -> str | None:
    """The ``type`` inside a message's ``provenance`` object, or None.

    ``None`` is the common answer and the meaningful one: in the recorded
    transcripts a newly-produced turn carries ``"provenance": null`` and a
    replayed or synthetic one carries an object.
    """
    provenance = message.get("provenance")
    if isinstance(provenance, Mapping):
        kind = provenance.get("type")
        return kind if isinstance(kind, str) else None
    return None


def decode_session_id(session_id: str) -> str | None:
    """The frame's ``session_id`` out of a ``clls_`` identifier.

    ``clls_`` is URL-safe base64 of ``{"v":1,"o":<org>,"p":<account>,
    "s":<session>}``. Decoding the listing is the documented way across:
    constructing the id works but leans on a versioned encoding and on
    an account uuid no frame carries. A shape we cannot decode is a miss,
    never an exception — the encoding is allowed to change under us.
    """
    if not session_id.startswith("clls_"):
        return None
    raw = session_id.removeprefix("clls_")
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        log.warning("compliance: undecodable session id (encoding changed?)")
        return None
    session = payload.get("s") if isinstance(payload, Mapping) else None
    return session if isinstance(session, str) else None


class ComplianceClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str = API_BASE,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}

    async def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._base}{path}", params=dict(params), headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise ComplianceError(f"{path}: {exc!r}") from exc
        if response.status_code // 100 != 2:
            raise ComplianceError(f"{path}: HTTP {response.status_code} {response.text[:200]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ComplianceError(f"{path}: unparseable body") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    # --- feed one: activities, ordered, resumable ---------------------

    async def iter_activities(
        self, *, since: datetime, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Oldest-first from ``since``. ``order=asc`` is mandatory."""
        params: dict[str, Any] = {
            "created_at.gte": since.isoformat(),
            "order": "asc",
            "limit": page_size,
        }
        while True:
            payload = await self._get(_ACTIVITIES, params)
            rows = _rows(payload)
            for row in rows:
                yield row
            if not payload.get("has_more") or not payload.get("last_id") or not rows:
                return
            params = {**params, "after_id": payload["last_id"]}

    # --- feed two: chats, ordered only when asked ---------------------

    async def iter_chats(
        self, *, since: datetime, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """``order_by`` is not optional here: the bound is rejected without it."""
        params: dict[str, Any] = {
            "updated_at.gte": since.isoformat(),
            "order_by": "updated_at",
            "limit": page_size,
        }
        while True:
            payload = await self._get(_CHATS, params)
            rows = _rows(payload)
            for row in rows:
                yield row
            if not payload.get("has_more") or not payload.get("last_id") or not rows:
                return
            params = {**params, "after_id": payload["last_id"]}

    # --- feed three: local sessions, unorderable ----------------------

    async def drain_local_sessions(self, *, since: datetime, limit: int) -> Drain:
        """Drain the lagging window. No ordering parameter exists, so this
        cannot stream forward from a watermark; it reads the window and
        leans on the pending store's tombstones to suppress repeats.

        ``limit`` bounds *sessions*, not pages: a tick that hits it leaves
        the oldest sessions unread and the returned ``complete`` is False.
        """
        # No `organization_uuid` here: both listings reject it as a query
        # parameter, so that filter is the caller's and runs over these
        # rows.
        params: dict[str, Any] = {"updated_at.gte": since.isoformat(), "limit": min(limit, 100)}
        sessions: list[dict[str, Any]] = []
        while True:
            payload = await self._get(_SESSIONS, params)
            rows = _rows(payload)
            for row in rows:
                if len(sessions) >= limit:
                    log.warning(
                        "compliance: local-session drain cut at %d; oldest sessions unread, "
                        "watermark stays put",
                        limit,
                    )
                    return Drain(sessions=sessions, complete=False)
                sessions.append(row)
            token = payload.get("next_page")
            if not token or not rows:
                return Drain(sessions=sessions, complete=True)
            params = {**params, "page": token}

    # --- transcripts and bytes ----------------------------------------

    async def chat(
        self, chat_id: str, *, tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES
    ) -> dict[str, Any]:
        """The chat object, turns included. Its ``model`` is the only one
        there is — no chat message carries one."""
        return await self._get(f"{_CHATS}/{chat_id}/messages", _tool_caps(tool_block_bytes))

    async def chat_messages(
        self, chat_id: str, *, tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES
    ) -> list[dict[str, Any]]:
        """A chat's turns, which sit under ``chat_messages`` rather than
        ``data``: reading ``data`` here yields nothing and says nothing
        about why."""
        return _chat_messages(await self.chat(chat_id, tool_block_bytes=tool_block_bytes))

    async def session_messages(
        self, session_id: str, *, tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES
    ) -> list[dict[str, Any]]:
        """A local-session transcript, ``{session, data, next_page}``.

        It paginates like the listing it came from rather than like the
        two ordered feeds, so this follows ``next_page`` to the end: a
        partial transcript silently hides produced turns.
        """
        params: dict[str, Any] = {**_tool_caps(tool_block_bytes), "limit": _MESSAGE_PAGE}
        messages: list[dict[str, Any]] = []
        while True:
            payload = await self._get(f"{_SESSIONS}/{session_id}/messages", params)
            rows = _rows(payload)
            messages.extend(rows)
            token = payload.get("next_page")
            if not token or not rows:
                return messages
            params = {**params, "page": token}

    async def file_content(self, file_id: str) -> bytes:
        """The whole stored body. There is no ``HEAD`` to size it first —
        measured: ``HEAD`` 404s on every attachment — so the listing's
        ``size_bytes`` is what decides whether this is called at all."""
        url = f"{self._base}{_FILE_CONTENT.format(file_id=file_id)}"
        try:
            response = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ComplianceError(f"file {file_id}: {exc!r}") from exc
        if response.status_code // 100 != 2:
            raise ComplianceError(f"file {file_id}: HTTP {response.status_code}")
        return response.content


def _tool_caps(tool_block_bytes: int) -> dict[str, Any]:
    """Both caps move together; ``-1`` lifts them to the ~1 MiB ceiling."""
    return {
        "tool_result_max_bytes": tool_block_bytes,
        "tool_use_input_max_bytes": tool_block_bytes,
    }


def _rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, Sequence):
        return []
    return [row for row in data if isinstance(row, dict)]


def _chat_messages(chat: Mapping[str, Any]) -> list[dict[str, Any]]:
    turns = chat.get("chat_messages")
    if not isinstance(turns, Sequence):
        return []
    return [turn for turn in turns if isinstance(turn, dict)]
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_client.py -v && uv run ty check`. Expected: 18 passed (3 yaml cases plus 15). If `test_feed_vocabulary` fails on an unexpected parameter, the adapter is leaking another feed's vocabulary — that assertion exists to catch a "harmless" shared helper being introduced later, and `filters_rejected.json` records what each feed does with one.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/compliance anthropic/tests/test_client.py \
        anthropic/tests/test_feed_vocabulary.yaml
git commit -m "feat(anthropic): compliance client with one adapter per feed"
```

### Task 7.4: promote `Checkpoint` and `CheckpointStore` into `shared/`

**Files:**
- Create: `shared/src/slashid_ai_forwarder_core/checkpoint.py`
- Move: `vertex/tests/test_firestore_checkpoint.py` → `shared/tests/test_checkpoint.py`
- Delete: `vertex/src/slashid_vertex_forwarder/checkpoint_store.py`
- Modify: `vertex/src/slashid_vertex_forwarder/{event_source,audit_only_source,main}.py`
- Modify: `vertex/tests/{test_bq_event_source,test_audit_only_event_source,test_handler}.py`

The readers need three watermarks and `vertex/` already has exactly the right type, so it moves rather than being copied. The real cost is a knot: `checkpoint_store.py` imports `Checkpoint` from `event_source.py`, while `event_source.py` imports `CheckpointStore` back from `checkpoint_store.py` (under `TYPE_CHECKING`, which is what has been hiding it). Promoting both halves into one shared module unties it — afterwards nothing in `vertex/` imports either name from a sibling.

`FirestoreCheckpointStore` moves too. It takes its client as `Any` and never imports `google.cloud`, so it carries no dependency into `shared/` — check that before moving, because it is the only reason this is a file move rather than a packaging change.

- [ ] **Step 1: Move the module** — `git mv` keeps the history and the test file with it:

```bash
git mv vertex/src/slashid_vertex_forwarder/checkpoint_store.py \
       shared/src/slashid_ai_forwarder_core/checkpoint.py
git mv vertex/tests/test_firestore_checkpoint.py shared/tests/test_checkpoint.py
```

Then, in `shared/src/slashid_ai_forwarder_core/checkpoint.py`, replace `from .event_source import Checkpoint` with the dataclass itself, lifted verbatim from `vertex/src/slashid_vertex_forwarder/event_source.py:166-177`:

```python
@dataclass(frozen=True)
class Checkpoint:
    """The polling watermark — ``(timestamp, id)`` of the last processed
    entry. Universal across event sources.

    ``timestamp = None`` means "no entries seen yet". What a source does
    with that is the source's decision, and the two in this repo differ:
    Vertex fetches every entry up to its batch bound, while the Anthropic
    compliance readers start at ``now - POLL_LAG`` instead, because a
    backfill there would re-emit the whole retention window.

    Always a timestamp, **never a feed's page token**: those are
    documented as format-unstable, and they paginate within one tick and
    are then discarded.
    """

    timestamp: datetime | None
    id: str | None
```

and add `from dataclasses import dataclass` to the imports. The docstring at the top of the module loses its Vertex-specific paragraph about `var.create_firestore_database`; `FirestoreCheckpointStore` keeps its own.

- [ ] **Step 2: Re-point every importer** — six files, all mechanical:

```bash
cd /home/paulo/slashid/slashid-ai-forwarder
# The definition leaves event_source.py; the protocol import goes with it.
sed -i 's|^from .checkpoint_store import FirestoreCheckpointStore$|from slashid_ai_forwarder_core.checkpoint import FirestoreCheckpointStore|' \
    vertex/src/slashid_vertex_forwarder/main.py
sed -i 's|^from .event_source import Checkpoint$|from slashid_ai_forwarder_core.checkpoint import Checkpoint|' \
    vertex/src/slashid_vertex_forwarder/audit_only_source.py
sed -i 's|from slashid_vertex_forwarder.event_source import Checkpoint|from slashid_ai_forwarder_core.checkpoint import Checkpoint|' \
    vertex/tests/test_handler.py vertex/tests/test_audit_only_event_source.py
grep -rn 'checkpoint_store\|event_source import.*Checkpoint' vertex/src vertex/tests
```

Four edits are left by hand because they are not one-line substitutions:

- `vertex/src/slashid_vertex_forwarder/event_source.py` — delete the `Checkpoint` dataclass, add `from slashid_ai_forwarder_core.checkpoint import Checkpoint, CheckpointStore` at the top, and delete the `if TYPE_CHECKING:` line that imported `CheckpointStore` from `.checkpoint_store`. That deletion is the cycle.
- `vertex/src/slashid_vertex_forwarder/audit_only_source.py` — the same `TYPE_CHECKING` import of `CheckpointStore`.
- `vertex/tests/test_bq_event_source.py:20` — `from slashid_vertex_forwarder.event_source import BqEventSource, Checkpoint` splits into the shared import plus `BqEventSource`.
- `shared/tests/test_checkpoint.py:13` — `from slashid_vertex_forwarder.checkpoint_store import …` becomes `from slashid_ai_forwarder_core.checkpoint import …`.

- [ ] **Step 3: Run both suites** — the move is behaviour-preserving, so this is the verification:

```bash
cd shared && uv run ruff check . && uv run ty check && uv run pytest -q
cd ../vertex && uv run ruff check . && uv run ty check && uv run pytest -q
```

Expected: `370 passed` in `shared` (364 plus the 6 that came with the file) and `147 passed` in `vertex` (153 minus those 6). A different number means an import was re-pointed to a name that no longer exists, and pytest names the file.

- [ ] **Step 4: Check nothing still reaches into vertex for it**

```bash
grep -rn 'checkpoint_store' --include='*.py' --include='*.toml' . | grep -v '\.venv'
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add shared vertex
git commit -m "refactor(shared): promote checkpoint out of vertex and untie the import cycle"
```

### Task 7.5: `checkpoint.py` — three independent cursors

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/compliance/checkpoint.py`
- Test: `anthropic/tests/test_cursors.py`

Three feeds, three watermarks, three documents — `vertex/` already constructs one store per source with a distinct `document=`, so the promoted constructor takes what this needs unchanged. Three rules live here, and each one is a way the readers lose data if it is not:

1. **Persist a timestamp, never a page token.** The tokens are format-unstable, and they mean nothing outside the tick that minted them.
2. **A cold start is `now - POLL_LAG`, not a backfill.** `load()` answers an empty checkpoint on the first tick, and Vertex's semantics for that are "fetch everything up to the batch bound" — here that would re-emit the entire retention window as standalone events the moment a credential is added. A backfill is an explicit opt-in, never an accident.
3. **A truncated drain does not advance.** The sessions listing is unorderable and newest-first, so the part a cap cuts off is the oldest. Advancing past it loses those sessions for good.

- [ ] **Step 1: Write the failing tests** — `anthropic/tests/test_cursors.py`:

```python
"""Three watermarks, and the three ways a reader loses data without them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from slashid_ai_forwarder_core.checkpoint import Checkpoint

from slashid_anthropic_forwarder.compliance.checkpoint import (
    ACTIVITIES,
    CHATS,
    SESSIONS,
    Cursors,
)

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
LAG = 120


class _FakeStore:
    def __init__(self) -> None:
        self.value = Checkpoint(None, None)
        self.saves: list[Checkpoint] = []

    def load(self) -> Checkpoint:
        return self.value

    def save(self, checkpoint: Checkpoint) -> None:
        self.value = checkpoint
        self.saves.append(checkpoint)


def _cursors() -> tuple[Cursors, dict[str, _FakeStore]]:
    stores = {feed: _FakeStore() for feed in (ACTIVITIES, CHATS, SESSIONS)}
    return Cursors(stores, poll_lag_seconds=LAG), stores


def test_a_cold_start_is_the_lag_window_not_a_backfill() -> None:
    cursors, _ = _cursors()
    assert cursors.window_start(SESSIONS, now=NOW) == NOW - timedelta(seconds=LAG)


def test_a_saved_watermark_is_resumed_verbatim() -> None:
    cursors, stores = _cursors()
    when = NOW - timedelta(hours=3)
    stores[ACTIVITIES].value = Checkpoint(timestamp=when, id="act_9")
    assert cursors.window_start(ACTIVITIES, now=NOW) == when


def test_advance_persists_a_timestamp_and_never_a_page_token() -> None:
    cursors, stores = _cursors()
    cursors.advance(ACTIVITIES, timestamp=NOW, id="act_9", drained=True)
    assert stores[ACTIVITIES].saves == [Checkpoint(timestamp=NOW, id="act_9")]


def test_a_truncated_drain_does_not_advance_the_watermark() -> None:
    # The listing is newest-first, so the sessions a cap leaves out are the
    # oldest. Advancing past them loses them permanently.
    cursors, stores = _cursors()
    before = cursors.window_start(SESSIONS, now=NOW)
    cursors.advance(SESSIONS, timestamp=NOW, drained=False)
    assert stores[SESSIONS].saves == []
    assert cursors.window_start(SESSIONS, now=NOW + timedelta(minutes=5)) >= before


def test_the_three_feeds_are_independent() -> None:
    cursors, stores = _cursors()
    cursors.advance(CHATS, timestamp=NOW, drained=True)
    assert stores[ACTIVITIES].saves == []
    assert stores[SESSIONS].saves == []


def test_window_age_is_reported_for_the_backlog_alarm() -> None:
    cursors, stores = _cursors()
    stores[SESSIONS].value = Checkpoint(timestamp=NOW - timedelta(hours=2), id=None)
    assert cursors.window_age_seconds(SESSIONS, now=NOW) == 7200
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_cursors.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.compliance.checkpoint'`.

- [ ] **Step 3: Implement**:

```python
"""Three independent cursors, one per compliance feed.

A ``(timestamp, id)`` watermark is a resumable cursor only on the two
ordered feeds. On local sessions it is a *window bound*: the listing
cannot be ordered, so the reader re-reads the window each tick and the
pending store's tombstones suppress what it already emitted. Saving
after a partial drain there is not a small inaccuracy — it silently
drops every session the cap left unread.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta

from slashid_ai_forwarder_core.checkpoint import Checkpoint, CheckpointStore

log = logging.getLogger(__name__)

ACTIVITIES = "compliance_activities"
CHATS = "compliance_chats"
SESSIONS = "compliance_local_sessions"
FEEDS = (ACTIVITIES, CHATS, SESSIONS)


class Cursors:
    """One ``CheckpointStore`` per feed, keyed by feed name.

    The stores are handed in rather than constructed here so the same
    class serves the Firestore-backed deployment and the tests.
    """

    def __init__(
        self, stores: Mapping[str, CheckpointStore], *, poll_lag_seconds: int
    ) -> None:
        self._stores = dict(stores)
        self._lag = timedelta(seconds=poll_lag_seconds)

    def window_start(self, feed: str, *, now: datetime) -> datetime:
        """The lower bound for this tick.

        An empty checkpoint means the credential was just added, and the
        answer is **not** Vertex's "fetch everything": a compliance
        backfill would re-emit the whole retention window as standalone
        events. Start one lag window back and let a backfill be an
        explicit decision someone makes on purpose.
        """
        saved = self._stores[feed].load()
        if saved.timestamp is None:
            return now - self._lag
        return saved.timestamp

    def advance(
        self,
        feed: str,
        *,
        timestamp: datetime,
        id: str | None = None,
        drained: bool,
    ) -> None:
        """Move the watermark, but only after a drain that finished."""
        if not drained:
            log.warning(
                "compliance: %s drain incomplete; watermark held at %s (window age %.0fs)",
                feed,
                self._stores[feed].load().timestamp,
                self.window_age_seconds(feed, now=timestamp),
            )
            return
        self._stores[feed].save(Checkpoint(timestamp=timestamp, id=id))

    def window_age_seconds(self, feed: str, *, now: datetime) -> float:
        """How far behind the watermark is. The backlog alarm reads this:
        if arrivals exceed ``MAX_SESSIONS_PER_TICK`` every tick the reader
        never catches up, and the window — not the tick duration — is what
        grows. It must stay under ``TOMBSTONE_TTL``, or tombstones expire
        before the reader re-walks and it re-emits."""
        saved = self._stores[feed].load()
        if saved.timestamp is None:
            return 0.0
        return (now - saved.timestamp).total_seconds()
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run pytest tests/test_cursors.py -v && uv run ruff check . && uv run ty check`. Expected: 6 passed. `id` shadows a builtin and ruff's `A` rules are not enabled here, so it stays — it is the field's name on `Checkpoint`.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/compliance/checkpoint.py \
        anthropic/tests/test_cursors.py
git commit -m "feat(anthropic): three compliance cursors, lagging and never backfilling"
```

### Task 7.6: `record.py` carries the digests a reader delivers

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/record.py`
- Test: `anthropic/tests/test_record.py`

Reader B cannot hand a completing `accessed_files` list to the store. It holds the listing's entries but not the frame's untruncated `tool_result` ones, and a merged list field replaces rather than unions — so a reader that sent `accessed_files` would overwrite the better digests with nothing. It delivers `file_digests` instead, under the same name the record's `awaiting` set already uses (`record.FILE_DIGESTS`), and the swap happens where the record becomes an event.

**That place is `to_event`, not `pending.py`.** `to_event` is the one function that turns the stored mapping into `AIInvocationObservedV1`, it already rewrites `parsed_as` from `contributed` on the way through, and both pushers reach the wire through it. Putting the swap beside `FILE_DIGESTS` in the same module keeps the expectation, its carrier field and its application in one file; putting it in `pending.py` would mean two call sites (`push_claimed` and nothing else today, more later) each remembering to apply it.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_record.py`:

```python
def _record_with(**over: Any) -> PendingRecord:
    base: dict[str, Any] = {
        "address": "toolu_01A",
        "event": {
            "request_id": "toolu_01A",
            "timestamp": "2026-09-20T23:08:20+00:00",
            "identity_details": {"kind": "anthropic", "user_id": "user_01A"},
            "model": {"id": "claude-opus-5"},
            "accessed_files": [
                {"name": "src/a.py", "content_hashes": {"sha256": "aa"},
                 "provenance": "tool_result"},
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
```

Extend that module's import block with `COMPLIANCE`, `PARSED_AS_COMPLIANCE`, `PARSED_AS_JOINED`, `from_document` and `to_event` as needed.

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_record.py -v`. Expected: `TypeError: PendingRecord.__init__() got an unexpected keyword argument 'file_digests'` on three of the four, and the fourth (`from_document`) failing on `AttributeError: 'PendingRecord' object has no attribute 'file_digests'`.

- [ ] **Step 3: Implement** — one field, one reader, one helper. On `PendingRecord`, beside `awaiting`:

```python
    # Attachment digests a reader delivered, applied when the record
    # becomes an event. They live beside the event rather than inside it
    # because a reader cannot merge into `accessed_files` — it holds the
    # listing's entries and not the frame's untruncated tool-result ones,
    # and a merged list field replaces rather than unions.
    file_digests: list[dict[str, Any]] = field(default_factory=list)
```

in `from_document`, beside `awaiting`:

```python
        file_digests=list(data.get("file_digests") or []),
```

and the helper, beside `FILE_DIGESTS`'s other users:

```python
def apply_file_digests(
    event: Mapping[str, Any], digests: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Swap the attachment group for what a reader measured.

    Pairing a frame's attachment block to a ``files[]`` entry is
    unreliable — ``file_name`` is null for images and was null for this
    tenant's PDFs, the ``<uploaded_files>`` order does not match the block
    order, and ``size_bytes`` disagrees whenever the stored copy was
    processed — so the group is replaced wholesale. ``tool_result``
    entries are never touched.

    No digests means no replacement, which is not the same as an empty
    group: a reader that visited and found nothing clears the expectation
    at the store, and the frame's own entries stay.
    """
    body = dict(event)
    if not digests:
        return body
    existing = body.get("accessed_files") or []
    body["accessed_files"] = [
        *[f for f in existing if f.get("provenance") != "attachment"],
        *digests,
    ]
    return body
```

and `to_event` grows one line:

```python
def to_event(record: PendingRecord) -> AIInvocationObservedV1:
    """Validate a record into the event that goes on the wire.

    The one place the stored mapping becomes an event, so it is also
    where a reader's digests are applied and where ``parsed_as`` is
    decided from ``contributed``.
    """
    return AIInvocationObservedV1.model_validate(
        {
            **apply_file_digests(record.event, record.file_digests),
            "parsed_as": parsed_as(record.contributed),
        }
    )
```

Add `Mapping` and `Sequence` to the `collections.abc` import.

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_record.py tests/test_pending.py -v && uv run ty check`. Expected: Chunk 5's 13 plus 4 here, and Chunk 6's `test_pending.py` unchanged and green — nothing above `to_event` moved.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/record.py anthropic/tests/test_record.py
git commit -m "feat(anthropic): apply a reader's attachment digests at push"
```

### Task 7.7: `attachments.py` — two tiers, decided before any fetch

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/compliance/attachments.py`
- Test: `anthropic/tests/test_attachments.py`

Neither the file id nor the md5 is in a frame — an `attachment` block carries only `file_name`, `media_type`, `size_bytes` and `text` — so both come from the chat message's `files[]`, whose entries are `id`, `filename`, `mime_type`, `size_bytes` and `md5` (lowercase hex). `md5` is the default tier and is free in both senses: the digest already rides in a response Reader B fetches anyway, and no file bytes transit the collector. `full` downloads and adds sha1 and sha256, which OneDrive, SharePoint and Drive resources need.

The cap is decided **from the listing**, before any fetch, because there is no cheap metadata call to fall back on: `HEAD` on the content endpoint 404s on every attachment. An oversized file is never started rather than aborted, and it keeps the listing's whole-file md5 — so the entry stays matchable and loses only sha1 and sha256. A partial read is never hashed.

The corpus gives all three cases in one message. `chat_messages_1.json`'s user turn lists `guiaSADT.pdf` (59430 B), a JPEG (72878 B) and `maria.txt` (27 B) — and `maria.txt`'s listed md5 is the md5 of the text `frame_attachment.json` carries, so the `full` tier can be checked against a digest recorded from the tenant rather than against the mock's own arithmetic. `chat_messages_2.json` adds the awkward one: a `mime_type` of `"txt"`, a bare extension rather than a media type, which is exactly what Chunk 2 Task 2.2 made `parse_media_type` degrade to `None` on instead of raising.

- [ ] **Step 1: Write the failing tests** — `anthropic/tests/test_attachments.py`:

```python
"""Attachment enrichment: the listing's md5, or the stored bytes."""

from __future__ import annotations

import hashlib

from slashid_ai_forwarder_core.events import AIAccessedFile

from slashid_anthropic_forwarder.compliance.attachments import (
    files_from_listing,
    listed_files,
)
from slashid_anthropic_forwarder.compliance.client import ComplianceClient
from tests.compliance_fixtures import MARIA_BYTES, body, transport
from tests.test_pending import config as a_config


def _uploads() -> dict:
    return next(
        m for m in body("chat_messages_1.json")["chat_messages"] if m.get("files")
    )


def a_client() -> tuple[ComplianceClient, list]:
    client, seen = transport()
    return ComplianceClient(client, api_key="k"), seen


def test_listed_files_reads_the_five_fields_and_lowercases_the_digest() -> None:
    entries = listed_files(_uploads())
    assert [e.filename for e in entries] == [
        "guiaSADT.pdf",
        "WhatsApp Image 2026-09-02 at 17.07.21.jpeg",
        "maria.txt",
    ]
    assert all(e.md5 == (e.md5 or "").lower() for e in entries)
    assert listed_files({"role": "user", "content": []}) == []


async def test_md5_tier_takes_the_listing_digest_and_makes_no_request() -> None:
    client, seen = a_client()
    entries = listed_files(_uploads())
    files = await files_from_listing(client, entries, config=a_config())
    assert seen == []
    assert [set(f.content_hashes or {}) for f in files] == [{"md5"}] * 3
    assert all(f.provenance == "attachment" for f in files)
    assert [f.byte_length for f in files] == [59430, 72878, 27]


async def test_full_tier_downloads_and_its_md5_equals_the_listing() -> None:
    client, seen = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.filename == "maria.txt")
    files = await files_from_listing(
        client, [entry], config=a_config(attachment_hashing="full")
    )
    assert len(seen) == 1
    hashes = files[0].content_hashes or {}
    assert set(hashes) == {"md5", "sha1", "sha256"}
    # The listing's md5 was recorded from the tenant; these bytes are the
    # frame's extracted text. Their agreeing is the measured claim.
    assert hashes["md5"] == entry.md5 == hashlib.md5(MARIA_BYTES).hexdigest()
    assert hashes["sha256"] == hashlib.sha256(MARIA_BYTES).hexdigest()


async def test_an_oversized_file_is_never_requested() -> None:
    # Decided from the listing's size_bytes before any fetch — there is no
    # HEAD to fall back on, so this is the only place to decide it.
    client, seen = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.mime_type == "image/jpeg")
    files = await files_from_listing(
        client,
        [entry],
        config=a_config(attachment_hashing="full", max_attachment_fetch_bytes=1024),
    )
    assert seen == []
    assert files[0].content_hashes == {"md5": entry.md5}


async def test_an_unknown_size_is_treated_as_over_the_cap() -> None:
    client, seen = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.filename == "maria.txt")
    sized = type(entry)(**{**entry.__dict__, "size_bytes": None})
    files = await files_from_listing(
        client, [sized], config=a_config(attachment_hashing="full")
    )
    assert seen == []
    assert files[0].content_hashes == {"md5": entry.md5}


async def test_a_missing_body_degrades_to_the_listing_md5() -> None:
    client, _ = a_client()
    entry = next(e for e in listed_files(_uploads()) if e.filename == "guiaSADT.pdf")
    files = await files_from_listing(
        client, [entry], config=a_config(attachment_hashing="full")
    )
    # The corpus holds no body for the PDF, so the transport 404s and the
    # entry keeps a whole-file digest instead of losing the entry.
    assert files[0].content_hashes == {"md5": entry.md5}


async def test_a_bare_extension_mime_type_degrades_to_none() -> None:
    # `"txt"` is not a media type. Chunk 2 Task 2.2 made parse_media_type
    # fall back rather than raise; this is the recorded value that needs it.
    client, _ = a_client()
    message = next(
        m for m in body("chat_messages_2.json")["chat_messages"] if m.get("files")
    )
    files = await files_from_listing(client, listed_files(message), config=a_config())
    assert files[0].media_type is None
    assert files[0].content_hashes is not None
```

`tests/test_pending.py`'s `config` helper takes keyword overrides already, so `a_config(attachment_hashing="full")` needs nothing new.

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_attachments.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.compliance.attachments'`.

- [ ] **Step 3: Implement**:

```python
"""Attachment enrichment — the one thing a frame can never supply.

A frame's ``attachment`` block carries extracted text and no bytes, so
the hook side has nothing to hash but that text: exact for plain text,
and unmatchable for anything Claude stored as a processed copy. The
compliance ``files[]`` listing carries the stored file's ``md5``, and
its content endpoint streams the stored bytes.

Two tiers:

* ``md5`` (default) — no extra request, no file bytes through the
  collector, and the digest is already in a response Reader B fetches
  anyway. Not a lesser tier: Salesforce-sourced graph resources carry
  md5 alone.
* ``full`` — one GET per attachment, adding sha1 and sha256 for
  OneDrive, SharePoint and Drive.

Every digest here is of **what Claude stored**, which is not always what
the user uploaded: a processed image, or a document kept as extracted
text. Such a hash will not match the original, and the README says so.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.normalized.media_types import parse_media_type

from ..config import Config
from .client import ComplianceClient, ComplianceError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ListedFile:
    """One ``files[]`` entry. The only cheap metadata there is: ``HEAD``
    on the content endpoint 404s on every attachment."""

    id: str
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    md5: str | None


def listed_files(message: Mapping[str, Any]) -> list[ListedFile]:
    """The uploads hanging off one message.

    ``files[]`` hangs off the single message that carried the upload, so
    an attachment is reported once, on the invocation that consumed it.
    Without that, a twenty-turn chat about one PDF would look like twenty
    accesses. ``generated_files`` and ``artifacts`` are deliberately not
    read here: the first is reserved for a ``generated`` provenance that
    is not emitted yet, and an artifact is the assistant's own output,
    which does not belong in a field a reviewer reads as ingress.
    """
    raw = message.get("files")
    if not isinstance(raw, Sequence):
        return []
    out: list[ListedFile] = []
    for entry in raw:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            continue
        digest = entry.get("md5")
        out.append(
            ListedFile(
                id=entry["id"],
                filename=entry.get("filename"),
                mime_type=entry.get("mime_type"),
                size_bytes=entry.get("size_bytes"),
                # Lowercase hex on the wire; normalized so a comparison
                # against a recomputed digest cannot fail on case.
                md5=digest.lower() if isinstance(digest, str) else None,
            )
        )
    return out


async def files_from_listing(
    client: ComplianceClient, entries: Iterable[ListedFile], *, config: Config
) -> list[AIAccessedFile]:
    """One ``AIAccessedFile`` per listed upload, at the configured tier."""
    out: list[AIAccessedFile] = []
    for entry in entries:
        hashes: dict[str, str] | None = {"md5": entry.md5} if entry.md5 else None
        if _should_fetch(entry, config):
            try:
                data = await client.file_content(entry.id)
            except ComplianceError as exc:
                # Degrade to the listing's md5 rather than losing the
                # entry: a whole-file digest we already hold is still
                # matchable, and this is enrichment, not the record.
                log.warning("compliance: attachment %s not fetched (%s)", entry.id, exc)
            else:
                hashes = {
                    "md5": hashlib.md5(data).hexdigest(),
                    "sha1": hashlib.sha1(data).hexdigest(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
        out.append(
            AIAccessedFile(
                name=entry.filename,
                content_hashes=hashes,
                # A recorded `mime_type` is sometimes a bare extension
                # ("txt"), which is not a media type; Chunk 2 made this
                # fall back to None rather than raise.
                media_type=parse_media_type(entry.mime_type),
                byte_length=entry.size_bytes,
                provenance="attachment",
            )
        )
    return out


def _should_fetch(entry: ListedFile, config: Config) -> bool:
    """Decided from the listing, before any request.

    There is no ``HEAD`` to size the object with, so ``size_bytes`` is
    the only pre-fetch signal there is. An unknown size is treated as
    over the cap: starting a fetch we cannot bound and then stopping it
    would leave a partial read, and a partial read is never hashed.
    """
    if config.attachment_hashing != "full":
        return False
    if entry.size_bytes is None:
        log.info("compliance: attachment %s has no size_bytes; keeping the listing md5", entry.id)
        return False
    return entry.size_bytes <= config.max_attachment_fetch_bytes
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_attachments.py -v && uv run ty check`. Expected: 7 passed. If the `full` case fails on the md5 comparison, the recorded listing and the bytes in `compliance_fixtures.py` have drifted apart — re-record, never hand-edit the digest.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/compliance/attachments.py \
        anthropic/tests/test_attachments.py
git commit -m "feat(anthropic): attachment digests from the listing, or the stored bytes"
```

### Task 7.8: `denials.py` — Reader A

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/compliance/denials.py`
- Test: `anthropic/tests/test_denials.py`

The simplest reader, because a denial needs no join heuristic: the activity's `request_id` **is** the `webhook-id`, and Chunk 6 filed the honoured denial under `deny_address(webhook_id)`. Reader A imports that function — it does not re-derive the prefix, because two definitions of one load-bearing key is how silent divergence happens — and looks the record up directly, never scanning `webhook_ids`.

Two properties make the round trip worth it. An activity exists **only when the block was honoured** — a shadow-mode deny records nothing — so the feed is the authoritative record of what was actually blocked, and `SLASHID_SHADOW_MODE` stays out of the correctness path. And the activity carries a **real client user agent**, `claude-cli/2.1.278 (external, sdk-cli)` in the recorded row, that no frame does.

Filtering is the other half of the job. Our own reads are audited as `compliance_api_accessed` — 48 of 68 rows in the measured window — and the recorded feed has ten types and exactly one denial, so the reader filters on the type rather than on its own `api_key_id`, which it would otherwise have to know.

`model` is absent from the activity. It comes from the conversation's transcript when one is available, and Reader B already read this tick's transcripts, so it hands over what it saw rather than making Reader A re-list. `"unknown"` otherwise — including when Reader B failed, which is why the map is a parameter and not a lookup.

- [ ] **Step 1: Write the failing tests** — `anthropic/tests/test_denials.py`. The store under these is Chunk 5's real `FirestorePendingStore` over its fake client, and the push goes through Chunk 6's `push_if_ready`, so what is tested is the path that will run:

```python
"""Reader A: denials from the activity feed."""

from __future__ import annotations

from datetime import UTC, datetime

from slashid_anthropic_forwarder.address import deny_address
from slashid_anthropic_forwarder.compliance.checkpoint import ACTIVITIES, Cursors
from slashid_anthropic_forwarder.compliance.client import ComplianceClient
from slashid_anthropic_forwarder.compliance.denials import read_denials
from slashid_anthropic_forwarder.store import Seen
from tests.compliance_fixtures import body, transport
from tests.test_cursors import LAG
from tests.test_cursors import _FakeStore as FakeCheckpoints
from tests.test_pending import Sink, a_store, config as a_config, seed

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
ORG = "11111111-1111-1111-1111-111111111111"


def the_denial() -> dict:
    return next(
        row
        for row in body("activities.json")["data"]
        if row["type"] == "inference_hooks_request_denied"
    )


def a_reader() -> tuple[ComplianceClient, Cursors]:
    client, _ = transport()
    return (
        ComplianceClient(client, api_key="k"),
        Cursors({ACTIVITIES: FakeCheckpoints()}, poll_lag_seconds=LAG),
    )


async def run(store, sink: Sink, *, models=None, organization_uuid=ORG):  # noqa: ANN001
    client, cursors = a_reader()
    return await read_denials(
        client,
        store=store,
        cursors=cursors,
        config=a_config(compliance_key="sk-ant-api01-x", organization_uuid=organization_uuid),
        http=sink.client(),
        models=models or {},
        now=NOW,
    )


async def test_everything_that_is_not_a_denial_is_filtered_out() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    rows = body("activities.json")["data"]
    assert counters.handled == 1
    assert counters.skipped_not_a_denial == len(rows) - 1


async def test_an_unrecorded_denial_is_emitted_standalone_and_tombstoned() -> None:
    # Hook down, or a rollout below 100%: the activity is the whole record,
    # because no surface keeps the content of a denied call.
    store, sink = a_store(), Sink()
    denial = the_denial()
    address = deny_address(denial["request_id"])
    counters = await run(store, sink)
    assert counters.emitted == 1
    assert sink.request_ids == [denial["request_id"]]
    pushed = sink.bodies[0]["events"][0]
    assert pushed["stop_reason"] == "guardrail_intervened"
    assert pushed["parsed_as"] == "anthropic-compliance"
    assert pushed["identity_details"]["user_id"] == denial["actor"]["user_id"]
    assert pushed["user_agent"].startswith("claude-cli/")
    assert pushed["conversation_id"] == denial["conversation_id"]
    assert pushed["model"]["id"] == "unknown"
    # The retire inside push_if_ready is what stops the next tick
    # re-emitting it.
    assert await store.seen(address) is Seen.TOMBSTONED


async def test_a_live_record_is_completed_rather_than_re_emitted() -> None:
    store, sink = a_store(), Sink()
    denial = the_denial()
    address = deny_address(denial["request_id"])
    await seed(store, address)
    counters = await run(store, sink)
    assert counters.completed == 1 and counters.emitted == 0
    pushed = sink.bodies[0]["events"][0]
    assert pushed["stop_reason"] == "guardrail_intervened"
    assert pushed["user_agent"].startswith("claude-cli/")
    # Both sources supplied a field, which is what `joined` means.
    assert pushed["parsed_as"] == "anthropic-joined"


async def test_a_tombstoned_denial_is_left_alone() -> None:
    store, sink = a_store(), Sink()
    address = deny_address(the_denial()["request_id"])
    await seed(store, address)
    await store.retire(address, "pushed", now=NOW)
    counters = await run(store, sink)
    assert counters.tombstoned == 1
    assert sink.bodies == []


async def test_model_falls_back_to_a_transcript_reader_b_already_read() -> None:
    store, sink = a_store(), Sink()
    denial = the_denial()
    await run(store, sink, models={denial["conversation_id"]: "claude-opus-5"})
    assert sink.bodies[0]["events"][0]["model"]["id"] == "claude-opus-5"


async def test_another_organizations_activity_is_skipped() -> None:
    # The key reads every linked organization; the binding is per org, and
    # `organization_uuid` is not a query parameter on any feed.
    store, sink = a_store(), Sink()
    counters = await run(store, sink, organization_uuid="other")
    assert counters.handled == 0 and counters.skipped_other_org == 1
    assert sink.bodies == []


async def test_the_watermark_advances_to_the_newest_row_seen() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    newest = max(row["created_at"] for row in body("activities.json")["data"])
    assert counters.newest is not None
    assert counters.newest.isoformat().startswith(newest[:19])
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_denials.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.compliance.denials'`.

- [ ] **Step 3: Implement**:

```python
"""Reader A — denials, from the activity feed.

A denied call produces no response and therefore no successor frame, so
its pending record can only be completed by this reader or flushed on
its deadline. Two things make the round trip worth it:

* an activity exists **only when the block was honoured**. A shadow-mode
  deny records nothing, so the feed is the authoritative record of what
  was actually blocked and ``SLASHID_SHADOW_MODE`` stays out of the
  correctness path.
* the activity carries a real client user agent (``claude-cli/2.1.278``)
  that no frame does.

It has no ``model``, so that comes from the conversation's transcript
when Reader B read one this tick, and ``"unknown"`` otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
)

from ..address import deny_address
from ..config import Config
from ..pending import push_if_ready
from ..record import COMPLIANCE, Append, PARSED_AS_COMPLIANCE, open_fields
from ..store import PendingStore, Seen
from .checkpoint import ACTIVITIES, Cursors
from .client import DENIED_ACTIVITY, ComplianceClient

log = logging.getLogger(__name__)


@dataclass
class DenialCounters:
    handled: int = 0
    completed: int = 0
    emitted: int = 0
    tombstoned: int = 0
    skipped_not_a_denial: int = 0
    skipped_other_org: int = 0
    dropped_no_identity: int = 0
    newest: datetime | None = None


async def read_denials(
    client: ComplianceClient,
    *,
    store: PendingStore,
    cursors: Cursors,
    config: Config,
    http: httpx.AsyncClient,
    models: Mapping[str, str],
    now: datetime,
) -> DenialCounters:
    """One pass over the activity feed from the saved watermark."""
    start = cursors.window_start(ACTIVITIES, now=now)
    counters = DenialCounters()
    newest = start
    last_id: str | None = None
    async for activity in client.iter_activities(since=start):
        created = _created_at(activity)
        if created:
            newest = max(newest, created)
        last_id = activity.get("id") or last_id
        if activity.get("type") != DENIED_ACTIVITY:
            # Our own reads land here as `compliance_api_accessed` — 48 of
            # 68 rows in the measured window — alongside eight other types
            # in the recorded one. Filtering by type keeps them all out
            # without the reader needing to know its own api_key_id.
            counters.skipped_not_a_denial += 1
            continue
        if activity.get("organization_uuid") != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        await _handle(
            activity,
            store=store,
            config=config,
            http=http,
            models=models,
            counters=counters,
        )
    cursors.advance(ACTIVITIES, timestamp=newest, id=last_id, drained=True)
    counters.newest = newest
    return counters


async def _handle(
    activity: Mapping[str, Any],
    *,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    models: Mapping[str, str],
    counters: DenialCounters,
) -> None:
    request_id = activity.get("request_id")
    if not isinstance(request_id, str):
        return
    counters.handled += 1
    address = deny_address(request_id)
    state = await store.seen(address)
    if state is Seen.TOMBSTONED:
        counters.tombstoned += 1
        return
    actor = activity.get("actor") or {}
    if state is Seen.LIVE:
        # The record already holds the content. This stamps what the feed
        # alone attests — that the block actually happened — and the agent
        # no frame carries. `contributed` is what turns the pushed event
        # into `anthropic-joined`; without it `to_event` would still call
        # a two-source record hook-sourced.
        outcome = await store.complete(
            address,
            {
                "event": {
                    "stop_reason": "guardrail_intervened",
                    "user_agent": actor.get("user_agent"),
                },
                "contributed": Append((COMPLIANCE,)),
            },
            (),
        )
        counters.completed += 1
        await push_if_ready(address, outcome, store=store, config=config, client=http)
        return
    user_id = actor.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        # The server rejects an Anthropic identity with no identifier.
        counters.dropped_no_identity += 1
        return
    event = _standalone(activity, user_id=user_id, models=models)
    # A real delivery id, so `webhook_ids` gets one: this is the same
    # `webhook-id` the hook would have filed, and a later frame revealing
    # the same delivery should land in the same list.
    outcome = await store.upsert(
        address,
        open_fields(event, webhook_id=request_id, contributed=COMPLIANCE),
        (),
    )
    counters.emitted += 1
    # No expectations, so the record is born ready: this claims, pushes and
    # retires, and the tombstone stops the next tick re-emitting it.
    await push_if_ready(address, outcome, store=store, config=config, client=http)


def _standalone(
    activity: Mapping[str, Any], *, user_id: str, models: Mapping[str, str]
) -> AIInvocationObservedV1:
    """The event when the hook never recorded this delivery.

    No surface carries the content of a denied call once the frame is
    gone, so the activity is the whole record: who, which conversation,
    which client, and that it was blocked.
    """
    actor = activity.get("actor") or {}
    conversation_id = activity.get("conversation_id")
    model = models.get(conversation_id or "", "unknown")
    return AIInvocationObservedV1(
        request_id=activity["request_id"],
        timestamp=activity["created_at"],
        identity_details=AnthropicIdentityDetails(user_id=user_id),
        model=AIModel(
            id=model,
            provider="anthropic",
            raw_model_id=None if model == "unknown" else model,
        ),
        # Overridden by `to_event` from `contributed`; set so the model
        # validates here, where a mistake is cheap to see.
        parsed_as=PARSED_AS_COMPLIANCE,
        stop_reason="guardrail_intervened",
        user_agent=actor.get("user_agent") or activity.get("surface"),
        conversation_id=conversation_id,
    )


def _created_at(activity: Mapping[str, Any]) -> datetime | None:
    raw = activity.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_denials.py -v && uv run ty check`. Expected: 7 passed. A failure on `anthropic-joined` means `contributed` did not append — check that the store's array transform ran, not the reader.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/compliance/denials.py \
        anthropic/tests/test_denials.py
git commit -m "feat(anthropic): reader a completes denials from the activity feed"
```

### Task 7.9: `responses.py` — Reader B, and the two walks

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/compliance/responses.py`
- Test: `anthropic/tests/test_responses.py`, `anthropic/tests/test_produced_runs.yaml`

The reader with the rule that rebuilt the design.

**It may only touch a joinable run.** `joinable_address(run)` answers the run's first `tool_use.id`, or `None`. `None` is the end of the matter: there is no reader-side fallback key, because the fallback that suggests itself — a digest over the transcript prefix — is precisely what the measurement killed (200 keys frame-side, 302 reader-side, **zero** in common, and still zero with every text block removed). An unjoinable run is the hook's, filed under `hook:` + a delivery id no reader can compute, and emitting it here would be a second event that first-completed-wins counts twice rather than merges. `session_messages_3.json` is that case in the corpus: one tool-free assistant turn.

**A produced turn is not a role.** In a local session the marker is `model` with no `provenance`: `client_asserted` replays, the `synthetic_marker` the client never sent, and `content_unavailable` turns (`not_captured`, `client_aborted`, `cmek_key_revoked`, `retention_elapsed`, `oversize`) are all skipped, and so is an unrecognized provenance — the schema says to tolerate unknown `type` values, and a turn we cannot classify is not one to emit. In the largest measured session only 4 of 433 assistant messages carried a `model`; in the recorded corpus, which is six short sessions, nearly all of them do. Both are the same rule, and the yaml table is where it is pinned rather than in a ratio assertion the corpus would fail.

**The chat walk is a different function.** A chat message has no `model` and no `provenance` — the model is on the chat object — and its `tool_result` blocks sit **inside** the assistant message. Feeding those to `AnthropicMessage`, the response-side union, raises a `ValidationError` that would take down the whole tick, so the answer is filtered to response-side blocks. Two consequences worth stating: the address still works, because `joinable_address` reads `tool_use` ids out of the request-side parse, and `used_tools` does not, because the spine attributes a round's results from the *next* user message and a chat keeps them in the same turn. That is a known gap, not an accident — the frame path is the one with the shape the attribution rule was written for.

Everything else is the ownership rule: ask `seen` first. **Live** → deliver `file_digests`, clear the expectation, push if that made it ready. **Tombstoned** → skip, which is what stops a compliance-only deployment re-emitting every turn once per tick it stays in the lagging window. **Absent** → emit standalone, an `upsert` that leaves a ready record plus `push_if_ready`, whose `retire(PUSHED)` leaves the tombstone.

- [ ] **Step 1: Write the failing tests** — `anthropic/tests/test_responses.py`:

```python
"""Reader B: one event per newly-produced turn, joinable only, two walks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.address import joinable_address
from slashid_anthropic_forwarder.compliance.checkpoint import CHATS, SESSIONS, Cursors
from slashid_anthropic_forwarder.compliance.client import ComplianceClient
from slashid_anthropic_forwarder.compliance.responses import (
    chat_turns,
    produced_runs,
    read_responses,
    to_anthropic,
)
from slashid_anthropic_forwarder.hook.frame import PromptFrame, split_transcript
from slashid_anthropic_forwarder.store import Seen
from tests.compliance_fixtures import PAIRED, body, transport
from tests.test_cursors import LAG
from tests.test_cursors import _FakeStore as FakeCheckpoints
from tests.test_pending import Sink, a_store, config as a_config, seed

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
ORG = "11111111-1111-1111-1111-111111111111"


def session_messages(n: int) -> list[dict[str, Any]]:
    return body(f"session_messages_{n}.json")["data"]


def addresses(n: int) -> list[str]:
    return [
        a
        for a in (joinable_address(to_anthropic(run.messages)) for run in
                  produced_runs(session_messages(n)))
        if a is not None
    ]


def a_reader(**cursor_stores: Any) -> tuple[ComplianceClient, Cursors, list]:
    client, seen = transport()
    stores = {SESSIONS: FakeCheckpoints(), CHATS: FakeCheckpoints(), **cursor_stores}
    return ComplianceClient(client, api_key="k"), Cursors(stores, poll_lag_seconds=LAG), seen


async def run(store, sink: Sink, cursors: Cursors | None = None, **over: Any):  # noqa: ANN001
    client, built, _ = a_reader()
    return await read_responses(
        client,
        store=store,
        cursors=cursors or built,
        config=a_config(
            compliance_key="sk-ant-api01-x", organization_uuid=ORG, **over
        ),
        http=sink.client(),
        now=NOW,
    )


@yaml_pytest(filename="test_produced_runs.yaml")
def test_produced_runs(messages: list[dict[str, Any]], expected_models: list[str]) -> None:
    assert [run.model for run in produced_runs(messages)] == expected_models


def test_the_recorded_synthetic_marker_is_not_a_turn() -> None:
    # Every recorded transcript opens with one, on a *user* message.
    first = session_messages(1)[0]
    assert first["provenance"] == {"type": "synthetic_marker"}
    assert all(run.index > 0 for run in produced_runs(session_messages(1)))


def test_the_address_matches_the_hook_path_over_the_paired_corpus() -> None:
    """The load-bearing claim, measured: a run's address computed from a
    captured frame equals the one computed from the stored transcript."""
    checked = 0
    for path in sorted(PAIRED.glob("*.json")):
        frame = PromptFrame.model_validate_json(path.read_bytes())
        from_frame = joinable_address(split_transcript(frame).assistant_run)
        if from_frame is None:
            continue  # an opening prompt, or a tool-free run: nothing to join
        session = int(path.name.split("_")[1])
        assert from_frame in addresses(session), path.name
        checked += 1
    assert checked >= 6


def test_a_tool_free_run_has_no_address_at_all() -> None:
    # session_messages_3 is one text-only turn. There is no reader-side
    # digest to fall back on, and inventing one would reintroduce the key
    # the 200/302/zero measurement ruled out.
    runs = produced_runs(session_messages(3))
    assert len(runs) == 1
    assert joinable_address(to_anthropic(runs[0].messages)) is None


async def test_an_unjoinable_run_is_left_to_the_hook() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    assert counters.unjoinable >= 1
    assert all(not rid.startswith("hook:") for rid in sink.request_ids)


async def test_a_joinable_turn_the_hook_never_saw_is_emitted_and_tombstoned() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    address = addresses(1)[0]
    assert counters.emitted >= 1
    assert address in sink.request_ids
    pushed = next(e for b in sink.bodies for e in b["events"] if e["request_id"] == address)
    assert pushed["parsed_as"] == "anthropic-compliance"
    assert pushed["stop_reason"] in {"tool_use", "end_turn"}
    assert pushed["tokens"]["input"] == 0
    # Identity is on the listing item: a transcript message carries only
    # type, id, role, created_at, provenance, model and content.
    listed = body("sessions_list.json")["data"][0]["user"]["id"]
    assert pushed["identity_details"]["user_id"] == listed
    assert await store.seen(address) is Seen.TOMBSTONED


async def test_a_live_record_is_enriched_rather_than_emitted() -> None:
    store, sink = a_store(), Sink()
    address = addresses(6)[0]
    await seed(store, address, "file_digests")
    counters = await run(store, sink)
    assert counters.enriched >= 1
    pushed = next(e for b in sink.bodies for e in b["events"] if e["request_id"] == address)
    # `joined` is the proof it went through `complete` with a `contributed`
    # append rather than being opened again as a reader-only record.
    assert pushed["parsed_as"] == "anthropic-joined"
    assert sink.request_ids.count(address) == 1


async def test_a_tombstoned_run_is_not_re_emitted() -> None:
    store, sink = a_store(), Sink()
    address = addresses(1)[0]
    await seed(store, address)
    await store.retire(address, "pushed", now=NOW)
    counters = await run(store, sink)
    assert counters.tombstoned >= 1
    assert address not in sink.request_ids


async def test_the_chats_feed_emits_too() -> None:
    # The walk that did not exist: a chat assistant turn carries its tool
    # results inline and its model on the chat object, and nothing else in
    # the suite would have caught either.
    store, sink = a_store(), Sink()
    counters = await run(store, sink)
    chat = body("chat_messages_2.json")
    turns = chat_turns(chat)
    assert turns and all(t.model == chat["model"] for t in turns)
    joinable = [
        a for a in (joinable_address(to_anthropic(t.messages)) for t in turns) if a
    ]
    assert joinable, "chat_messages_2 has tool calls; if not, the fixture changed"
    assert counters.from_chats >= 1
    assert any(rid in sink.request_ids for rid in joinable)


def test_an_inline_tool_result_never_reaches_the_response_union() -> None:
    # Handing a chat assistant message's blocks to AnthropicMessage raises,
    # and inside a tick that takes every reader behind it down.
    chat = body("chat_messages_2.json")
    turn = next(t for t in chat_turns(chat) if any(
        block.get("type") == "tool_result"
        for message in t.messages
        for block in message["content"]
    ))
    from slashid_anthropic_forwarder.compliance.responses import response_blocks

    kinds = {b["type"] for b in response_blocks(turn.messages[0]["content"])}
    assert "tool_result" not in kinds
    assert kinds <= {"text", "tool_use", "thinking"}


async def test_a_truncated_drain_leaves_the_sessions_watermark_alone() -> None:
    sessions, chats = FakeCheckpoints(), FakeCheckpoints()
    cursors = Cursors({SESSIONS: sessions, CHATS: chats}, poll_lag_seconds=LAG)
    await run(a_store(), Sink(), cursors, max_sessions_per_tick=1)
    assert sessions.saves == []
    # The ordered feed is unaffected: it resumes from its own watermark.
    assert chats.saves != []


async def test_another_organizations_conversation_is_skipped() -> None:
    store, sink = a_store(), Sink()
    counters = await run(store, sink, organization_uuid="other")
    assert counters.emitted == 0
    assert counters.skipped_other_org == len(body("sessions_list.json")["data"]) + len(
        body("chats_list.json")["data"]
    )


def test_content_unavailable_and_replayed_turns_are_skipped() -> None:
    # Hand-written: this tenant has no retention policy in force and
    # produced none. A customer with finite retention does, and emitting
    # one would create a contentless invocation.
    messages = [
        {"role": "assistant", "model": "claude-opus-5",
         "provenance": {"type": "content_unavailable", "reason": "retention_elapsed"},
         "content": []},
        {"role": "assistant", "model": "claude-opus-5",
         "provenance": {"type": "client_asserted"}, "content": []},
    ]
    assert produced_runs(messages) == []


def test_unknown_blocks_survive_translation() -> None:
    translated = to_anthropic(
        [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "future"}]}]
    )
    assert len(translated) == 1 and len(translated[0].content) >= 1
```

`anthropic/tests/test_produced_runs.yaml`:

```yaml
# The anchor: a local-session turn is newly produced when it carries a
# model and no provenance at all. Every recorded transcript looks like
# this; the largest measured session had 4 such turns out of 433
# assistant messages, and the rule is the same either way.
id: a_marked_assistant_turn_with_no_provenance_is_a_run
messages:
  - {role: user, provenance: {type: synthetic_marker}, content: [{type: text, text: marker}]}
  - {role: user, content: [{type: text, text: hi}]}
  - {role: assistant, model: claude-opus-5, content: [{type: text, text: fresh}]}
expected_models: [claude-opus-5]
---
# One answer delivered in pieces is one run: a follower joins only when it
# carries neither a marker of its own nor a provenance.
id: an_answer_split_across_messages_is_one_run
messages:
  - {role: assistant, model: claude-opus-5, content: [{type: text, text: part one}]}
  - {role: assistant, content: [{type: tool_use, id: toolu_01A, name: Read, input: {}}]}
expected_models: [claude-opus-5]
---
# Replayed history is the overwhelming majority of what a long transcript
# holds, and it is not traffic.
id: client_asserted_history_is_not_a_turn
messages:
  - {role: assistant, model: claude-opus-5, provenance: {type: client_asserted}, content: []}
expected_models: []
---
# The synthetic marker the client never sent: one of the three reasons a
# digest over the transcript prefix cannot join the two sources.
id: the_synthetic_marker_is_not_a_turn
messages:
  - {role: assistant, model: claude-opus-5, provenance: {type: synthetic_marker}, content: []}
expected_models: []
---
# Tolerate unrecognized `type` values, says the schema. A turn we cannot
# classify is not one we emit.
id: an_unknown_provenance_is_skipped_not_rejected
messages:
  - {role: assistant, model: claude-opus-5, provenance: {type: future_kind}, content: []}
expected_models: []
---
# No marker at all: replayed history in a transcript whose turns predate
# the model field.
id: an_unmarked_assistant_message_is_not_a_turn
messages:
  - {role: assistant, content: [{type: text, text: replayed}]}
expected_models: []
---
# Two produced turns in one transcript, which is what every recorded
# local session looks like.
id: two_produced_turns
messages:
  - {role: assistant, model: claude-opus-5, content: [{type: text, text: one}]}
  - {role: user, content: [{type: text, text: next}]}
  - {role: assistant, model: claude-sonnet-5, content: [{type: text, text: two}]}
expected_models: [claude-opus-5, claude-sonnet-5]
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_responses.py -v`. Expected: collection ERROR, `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.compliance.responses'`.

- [ ] **Step 3: Implement**:

```python
"""Reader B — responses, from the stored transcripts.

One invocation per newly-produced assistant turn, and **only for a
joinable run**. ``joinable_address`` answers the run's first
``tool_use.id`` or ``None``, and ``None`` ends it: the fallback that
suggests itself — a digest over the transcript prefix — is exactly the
key the measurement ruled out (200 keys frame-side, 302 here, zero in
common, and still zero with every text block removed; the stored
transcript prepends a synthetic marker, carries turns from before
capture began, and includes sub-agent turns no frame shows). An
unjoinable run belongs to the hook, which already reported it under a
delivery id no reader can compute.

Two walks, because the feeds are different shapes:

* a **local session** marks a produced turn with ``model`` and no
  ``provenance``; its tool results arrive in the next user message.
* a **chat** has no per-message model — the chat object carries it — and
  keeps ``tool_result`` blocks *inside* the assistant message. Those
  blocks are not in the response-side union, so handing them to
  ``AnthropicMessage`` raises, and inside a tick that takes down every
  reader behind it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from slashid_ai_forwarder_core.events import (
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
    EventEnvelope,
    build_event_from_normalized,
)
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    message_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicRequestMessage,
    AnthropicToolUseBlock,
)
from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared

from ..address import joinable_address
from ..config import Config
# The accessed-files recipe is the spine's: a file a verdict allowed must
# not be recorded under a different digest, so this imports the hook's
# function rather than deriving a second one.
from ..hook.envelope import accessed_files_for
from ..pending import push_if_ready
from ..record import COMPLIANCE, FILE_DIGESTS, Append, PARSED_AS_COMPLIANCE, event_fields
from ..store import PendingStore, Seen
from .attachments import files_from_listing, listed_files
from .checkpoint import CHATS, SESSIONS, Cursors
from .client import ComplianceClient, decode_session_id, provenance_type

log = logging.getLogger(__name__)

# Blocks an assistant turn may carry on the response side. A chat keeps
# its tool_results in the same message, and AnthropicMessage will not
# validate one.
_RESPONSE_KINDS = frozenset({"text", "tool_use", "thinking"})


@dataclass
class ProducedRun:
    index: int
    messages: list[dict[str, Any]]
    model: str


@dataclass
class ResponseCounters:
    emitted: int = 0
    enriched: int = 0
    tombstoned: int = 0
    unjoinable: int = 0
    skipped_other_org: int = 0
    from_chats: int = 0
    dropped_no_identity: int = 0
    models: dict[str, str] = field(default_factory=dict)


def produced_runs(messages: Sequence[Mapping[str, Any]]) -> list[ProducedRun]:
    """Newly-produced turns in a **local session** transcript.

    The marker is ``model`` with no ``provenance``: a replayed
    ``client_asserted`` turn, the ``synthetic_marker`` the client never
    sent and a ``content_unavailable`` turn all carry one, and so does
    anything Anthropic adds later — which is the right default, since a
    turn we cannot classify is not one to emit.
    """
    runs: list[ProducedRun] = []
    for i, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("model"):
            continue
        if provenance_type(message) is not None:
            continue
        run = [dict(message)]
        for follower in messages[i + 1 :]:
            # One answer can arrive as several assistant messages. A
            # follower joins only when it carries neither a marker of its
            # own nor a provenance — anything marked is a different turn.
            if (
                follower.get("role") != "assistant"
                or follower.get("model")
                or follower.get("provenance")
            ):
                break
            run.append(dict(follower))
        runs.append(ProducedRun(index=i, messages=run, model=str(message["model"])))
    return runs


def chat_turns(chat: Mapping[str, Any]) -> list[ProducedRun]:
    """Produced turns in a **chat**, which are simply its assistant turns.

    A chat transcript is the canonical store rather than a client's
    replay, so there is no history to filter and no per-message marker to
    filter it with: no chat message carries ``model`` or ``provenance``,
    and the model is on the chat object.
    """
    model = str(chat.get("model") or "unknown")
    turns: list[ProducedRun] = []
    for i, message in enumerate(chat.get("chat_messages") or []):
        if message.get("role") == "assistant":
            turns.append(ProducedRun(index=i, messages=[dict(message)], model=model))
    return turns


def response_blocks(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The subset of an answer that the response-side union admits.

    A chat's assistant message carries its ``tool_result`` blocks inline,
    beside the ``tool_use`` that asked for them. ``AnthropicMessage`` does
    not model that, so passing them through raises a ``ValidationError``
    mid-tick. The results are not lost from the record — they are in the
    transcript this run is attributed against — only from the *answer*.
    """
    return [dict(b) for b in blocks if b.get("type") in _RESPONSE_KINDS]


def to_anthropic(messages: Sequence[Mapping[str, Any]]) -> list[AnthropicRequestMessage]:
    """Compliance messages → the canonical schema the spine speaks.

    The address must be byte-identical to the hook's for the same run, so
    both sides hand ``joinable_address`` the same type. The request-side
    union admits ``tool_result`` in either role, so a chat's inline
    results survive here; blocks it does not model fall through as
    ``AnthropicUnknownBlock`` and are skipped downstream, never rejected.
    """
    out: list[AnthropicRequestMessage] = []
    for message in messages:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = message.get("content")
        out.append(
            AnthropicRequestMessage.model_validate(
                {"role": role, "content": content if isinstance(content, list) else []}
            )
        )
    return out


async def read_responses(
    client: ComplianceClient,
    *,
    store: PendingStore,
    cursors: Cursors,
    config: Config,
    http: httpx.AsyncClient,
    now: datetime,
) -> ResponseCounters:
    """One pass over both conversation feeds."""
    counters = ResponseCounters()
    lag = timedelta(seconds=config.poll_lag_seconds)

    drain = await client.drain_local_sessions(
        since=cursors.window_start(SESSIONS, now=now), limit=config.max_sessions_per_tick
    )
    for session in drain.sessions:
        if session.get("organization_uuid") != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        session_id = session.get("id", "")
        messages = await client.session_messages(session_id)
        await _walk(
            produced_runs(messages),
            messages=messages,
            conversation_id=decode_session_id(session_id) or session_id,
            # A message carries no user; the listing item does.
            user_id=_listed_user_id(session),
            surface=session.get("product_surface"),
            client=client,
            store=store,
            config=config,
            http=http,
            counters=counters,
        )
    # Only a finished drain may move a window bound whose listing is
    # newest-first: the tail a cap leaves is the oldest.
    cursors.advance(SESSIONS, timestamp=now - lag, drained=drain.complete)

    async for listed in client.iter_chats(since=cursors.window_start(CHATS, now=now)):
        if listed.get("organization_uuid") != config.organization_uuid:
            counters.skipped_other_org += 1
            continue
        chat = await client.chat(listed.get("id", ""))
        messages = list(chat.get("chat_messages") or [])
        before = counters.emitted + counters.enriched
        await _walk(
            chat_turns(chat),
            messages=messages,
            conversation_id=str(chat.get("id") or ""),
            user_id=_listed_user_id(chat) or _listed_user_id(listed),
            surface="claude-ai",
            client=client,
            store=store,
            config=config,
            http=http,
            counters=counters,
        )
        counters.from_chats += (counters.emitted + counters.enriched) - before
    cursors.advance(CHATS, timestamp=now - lag, drained=True)
    return counters


async def _walk(
    runs: Sequence[ProducedRun],
    *,
    messages: Sequence[Mapping[str, Any]],
    conversation_id: str,
    user_id: str | None,
    surface: str | None,
    client: ComplianceClient,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    counters: ResponseCounters,
) -> None:
    for run in runs:
        counters.models[conversation_id] = run.model
        address = joinable_address(to_anthropic(run.messages))
        if address is None:
            # Owned by the hook, under a key no reader can compute. There
            # is no second-best address here on purpose.
            counters.unjoinable += 1
            continue
        state = await store.seen(address)
        if state is Seen.TOMBSTONED:
            counters.tombstoned += 1
            continue
        digests = await _digests(messages[: run.index], client=client, config=config)
        if state is Seen.LIVE:
            outcome = await store.complete(
                address,
                {
                    "file_digests": [d.model_dump(mode="json", exclude_none=True) for d in digests],
                    "contributed": Append((COMPLIANCE,)),
                },
                (FILE_DIGESTS,),
            )
            counters.enriched += 1
            await push_if_ready(address, outcome, store=store, config=config, client=http)
            continue
        event = await _standalone(
            messages,
            run=run,
            address=address,
            conversation_id=conversation_id,
            user_id=user_id,
            surface=surface,
            digests=digests,
            config=config,
        )
        if event is None:
            counters.dropped_no_identity += 1
            continue
        # `event_fields`, not `open_fields`: there is no delivery id on
        # this side, and `webhook_ids` is the list Reader A matches a
        # denial against — putting a `clsm_` id in it would be a lie.
        outcome = await store.upsert(
            address, {**event_fields(event), "contributed": Append((COMPLIANCE,))}, ()
        )
        counters.emitted += 1
        # No expectations, so the record is born ready: this pushes it and
        # the retire inside leaves the tombstone the next tick honours.
        await push_if_ready(address, outcome, store=store, config=config, client=http)


async def _digests(
    before: Sequence[Mapping[str, Any]], *, client: ComplianceClient, config: Config
) -> list[Any]:
    """Listing-derived entries for the round the run consumed."""
    entries = [f for message in _last_round(before) for f in listed_files(message)]
    return await files_from_listing(client, entries, config=config)


def _last_round(messages: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            return list(messages[i + 1 :])
    return list(messages)


async def _standalone(
    messages: Sequence[Mapping[str, Any]],
    *,
    run: ProducedRun,
    address: str,
    conversation_id: str,
    user_id: str | None,
    surface: str | None,
    digests: Sequence[Any],
    config: Config,
) -> AIInvocationObservedV1 | None:
    """The event for a joinable turn the hook never saw.

    Unsampled under a partial rollout, arriving while the receiver was
    down, or a session's final round. Worse than a hook-emitted event —
    10 KB-capped tool blocks, no untruncated digests — and far better
    than nothing; ``parsed_as`` says which it is.
    """
    if not user_id:
        # The server rejects an Anthropic identity with no identifier.
        return None
    before = to_anthropic(messages[: run.index])
    answer = response_blocks(
        [block for message in run.messages for block in (message.get("content") or [])]
    )
    response = AnthropicMessage.model_validate(
        {
            "type": "message",
            "role": "assistant",
            "content": answer,
            # No surface carries a stop reason, so it is inferred from
            # block shape — exactly as the hook path infers it.
            "stop_reason": "tool_use" if answer and answer[-1]["type"] == "tool_use" else "end_turn",
        }
    )
    normalized = await message_to_normalized_invocation(
        AnthropicRequestBody(messages=before), response, config=config
    )
    files = await accessed_files_for(before, config=config)
    normalized.accessed_files = [
        *[f for f in files if f.provenance != "attachment"],
        *digests,
    ]
    names: dict[str, None] = {}
    for message in [*before, *to_anthropic(run.messages)]:
        for block in message.content:
            if isinstance(block, AnthropicToolUseBlock):
                names.setdefault(block.name)
    tools, servers = build_tools_declared((name, None, None) for name in names)
    normalized.input.tools_declared = tools
    normalized.input.tool_servers = servers
    return await build_event_from_normalized(
        normalized,
        EventEnvelope(
            request_id=address,
            timestamp=str(run.messages[0].get("created_at") or ""),
            identity_details=AnthropicIdentityDetails(user_id=user_id),
            model=AIModel(id=run.model, provider="anthropic", raw_model_id=run.model),
            # Overridden by `to_event` from `contributed`.
            parsed_as=PARSED_AS_COMPLIANCE,
            user_agent=surface,
            conversation_id=conversation_id,
        ),
        config=config,
    )


def _listed_user_id(item: Mapping[str, Any]) -> str | None:
    """``user.id`` off a **listing item**, never off a message.

    A transcript message carries only ``type, id, role, created_at,
    provenance, model, content`` — there is no identity in it. That id is
    byte-identical to the frame's ``actor.id``, which keeps a
    reader-emitted event and a hook-emitted one on one graph identity
    instead of forking the same human in two.
    """
    user = item.get("user")
    if isinstance(user, Mapping) and isinstance(user.get("id"), str):
        return user["id"]
    return None
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_responses.py -v && uv run ty check`. Expected: 21 passed (7 yaml cases plus 14). Two failure modes worth recognising: a `ValidationError` naming `tool_result` means the chat answer reached `AnthropicMessage` unfiltered, and an address mismatch in the paired test is the translation, not the addressing — `to_anthropic` must hand `joinable_address` the same blocks the frame side does, and a dropped `tool_use` changes the anchor.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/compliance/responses.py \
        anthropic/tests/test_responses.py anthropic/tests/test_produced_runs.yaml
git commit -m "feat(anthropic): reader b emits and enriches joinable runs only"
```

### Task 7.10: wire the readers into `/tick`

**Files:**
- Create: `anthropic/src/slashid_anthropic_forwarder/compliance/readers.py`
- Modify: `anthropic/src/slashid_anthropic_forwarder/main.py`
- Test: `anthropic/tests/test_readers.py`, `anthropic/tests/test_main.py`

Nothing so far constructs a `ComplianceClient`, the three checkpoint cursors, or runs anything: `POST /tick` calls `flush_due` and returns. This is the task that makes the chunk reachable, and it settles four things.

**Order.** Reader B first, then Reader A, then the flush. B builds the `conversation_id → model` map that A's fallback needs, and both run before the flush because a reader's `complete` can make a record ready, and a record that became ready on this tick should go out on this tick rather than waiting for the next.

**Isolation.** A reader failure is a log line, never a failed tick: the two readers are independent sources and a 429 on the activity feed must not cost the responses that already landed. Cloud Scheduler retries a failed tick, which would re-run a reader that already advanced its watermark, so failing the request is worse than useless.

**The store is async and the checkpoints are not.** `CheckpointStore.load`/`save` came from `vertex/`, where everything is synchronous, and they stay that way — the alternative is an async protocol that only this service would use. That means six blocking single-document Firestore calls per tick, on a route with no latency budget: the verdict path never touches a checkpoint, and `/tick` is a scheduler job. If a tick ever does block long enough to matter, `asyncio.to_thread` around the two `Cursors` methods is the escape hatch, and it is one line in this module rather than a change to the promoted type.

**Two clients, deliberately.** The compliance client shares the tick's `httpx.AsyncClient` — every header it sends is per request — while the checkpoints need a synchronous `firestore.Client` alongside the pending store's `AsyncClient`. Both point at the same named database and different collections.

- [ ] **Step 1: Write the failing tests** — `anthropic/tests/test_readers.py`:

```python
"""The tick's reader pass: order, isolation, and the flush that follows."""

from __future__ import annotations

from datetime import UTC, datetime

from slashid_anthropic_forwarder.compliance import readers
from slashid_anthropic_forwarder.compliance.checkpoint import FEEDS, Cursors
from tests.compliance_fixtures import transport
from tests.test_cursors import _FakeStore as FakeCheckpoints
from tests.test_pending import Sink, a_store, config as a_config

NOW = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
ORG = "11111111-1111-1111-1111-111111111111"


def cursors() -> Cursors:
    return Cursors({feed: FakeCheckpoints() for feed in FEEDS}, poll_lag_seconds=120)


def enabled(**over):  # noqa: ANN201, ANN003
    return a_config(compliance_key="sk-ant-api01-x", organization_uuid=ORG, **over)


async def test_without_a_key_the_readers_do_not_run() -> None:
    client, seen = transport()
    counters = await readers.run_readers(
        store=a_store(), config=a_config(), http=client, cursors=cursors(), now=NOW
    )
    assert counters == {}
    assert seen == []


async def test_reader_b_runs_before_reader_a_and_feeds_it_the_models() -> None:
    client, seen = transport()
    counters = await readers.run_readers(
        store=a_store(), config=enabled(), http=Sink().client(), cursors=cursors(), now=NOW,
        compliance=client,
    )
    paths = [r.url.path for r in seen]
    assert paths.index("/v1/compliance/apps/sessions/local") < paths.index(
        "/v1/compliance/activities"
    )
    assert counters["denials_handled"] == 1
    assert counters["responses_emitted"] >= 1


async def test_a_failing_reader_b_does_not_stop_reader_a(monkeypatch) -> None:  # noqa: ANN001
    async def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("429")

    monkeypatch.setattr(readers, "read_responses", boom)
    client, _ = transport()
    counters = await readers.run_readers(
        store=a_store(), config=enabled(), http=Sink().client(), cursors=cursors(), now=NOW,
        compliance=client,
    )
    # Reader A still ran, with an empty model map — the documented "unknown".
    assert counters["denials_handled"] == 1
    assert "responses_emitted" not in counters


async def test_a_failing_reader_a_does_not_fail_the_pass(monkeypatch) -> None:  # noqa: ANN001
    async def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("429")

    monkeypatch.setattr(readers, "read_denials", boom)
    client, _ = transport()
    counters = await readers.run_readers(
        store=a_store(), config=enabled(), http=Sink().client(), cursors=cursors(), now=NOW,
        compliance=client,
    )
    assert counters["responses_emitted"] >= 1
```

and in `anthropic/tests/test_main.py`, two cases on the route:

```python
async def test_tick_runs_the_readers_before_the_flush(monkeypatch) -> None:  # noqa: ANN001
    order: list[str] = []

    async def fake_readers(**kwargs):  # noqa: ANN003, ANN202
        order.append("readers")
        return {"responses_emitted": 2}

    async def fake_flush(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        order.append("flush")
        return 1

    monkeypatch.setattr(main, "run_readers", fake_readers)
    monkeypatch.setattr(main, "flush_due", fake_flush)
    async with _client(store=a_store()) as client:
        response = await client.post("/tick")
    assert order == ["readers", "flush"]
    assert response.json() == {"flushed": 1, "responses_emitted": 2}


async def test_a_reader_failure_does_not_fail_the_tick(monkeypatch) -> None:  # noqa: ANN001
    async def boom(**kwargs):  # noqa: ANN003, ANN202
        raise RuntimeError("firestore down")

    monkeypatch.setattr(main, "run_readers", boom)
    async with _client(store=a_store()) as client:
        response = await client.post("/tick")
    # Cloud Scheduler retries a failed tick, which would re-run a reader
    # that already moved its watermark. The flush still ran.
    assert response.status_code == 200
    assert response.json()["flushed"] == 0
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_readers.py tests/test_main.py -v`. Expected: `ModuleNotFoundError: No module named 'slashid_anthropic_forwarder.compliance.readers'`, and in `test_main.py` `AttributeError: <module 'slashid_anthropic_forwarder.main'> has no attribute 'run_readers'`.

- [ ] **Step 3: Implement** `compliance/readers.py`:

```python
"""One reader pass, and what builds it.

Reader B runs first: it reads the transcripts, which is where the model
of a denied conversation is, and Reader A has no ``model`` of its own.
Both run before the flush, because a reader's ``complete`` can make a
record ready and a record that became ready on this tick should go out
on this tick.

Each reader is isolated. They are independent sources, a 429 on one feed
must not cost what the other already landed, and Cloud Scheduler retries
a failed tick — which would re-run a reader that already advanced its
watermark. So a reader failure is a log line and a missing counter.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx

from ..config import Config
from ..store import PendingStore
from .checkpoint import FEEDS, Cursors
from .client import ComplianceClient
from .denials import read_denials
from .responses import read_responses

log = logging.getLogger(__name__)


def build_cursors(config: Config) -> Cursors:
    """One checkpoint document per feed, in their own collection.

    Synchronous, because the promoted ``CheckpointStore`` is: six
    single-document reads and writes per tick, on a route with no latency
    budget. ``asyncio.to_thread`` around the two ``Cursors`` methods is
    the escape hatch if that ever stops being true.
    """
    from google.cloud import firestore

    client = firestore.Client(
        project=config.gcp_project_id, database=config.firestore_database
    )
    from slashid_ai_forwarder_core.checkpoint import FirestoreCheckpointStore

    return Cursors(
        {
            feed: FirestoreCheckpointStore(
                client=client, collection=config.checkpoint_collection, document=feed
            )
            for feed in FEEDS
        },
        poll_lag_seconds=config.poll_lag_seconds,
    )


async def run_readers(
    *,
    store: PendingStore,
    config: Config,
    http: httpx.AsyncClient,
    cursors: Cursors | None = None,
    compliance: httpx.AsyncClient | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Both readers, in order, each isolated. Counters for the tick's log.

    ``compliance`` overrides the transport the API calls go over; by
    default they share the tick's client, since every header this client
    sends is per request.
    """
    if not config.compliance_enabled or not config.compliance_key:
        return {}
    moment = now or datetime.now(UTC)
    client = ComplianceClient(compliance or http, api_key=config.compliance_key)
    cursors = cursors or build_cursors(config)
    counters: dict[str, int] = {}

    models: dict[str, str] = {}
    try:
        responses = await read_responses(
            client, store=store, cursors=cursors, config=config, http=http, now=moment
        )
    except Exception:
        log.exception("compliance: reader B failed; reader A continues with no model map")
    else:
        models = responses.models
        counters |= {
            "responses_emitted": responses.emitted,
            "responses_enriched": responses.enriched,
            "responses_unjoinable": responses.unjoinable,
        }

    try:
        denials = await read_denials(
            client,
            store=store,
            cursors=cursors,
            config=config,
            http=http,
            models=models,
            now=moment,
        )
    except Exception:
        log.exception("compliance: reader A failed")
    else:
        counters |= {
            "denials_handled": denials.handled,
            "denials_emitted": denials.emitted,
            "denials_completed": denials.completed,
        }
    return counters
```

and in `main.py`, the import and the route:

```python
from .compliance.readers import run_readers
```

```python
    @app.post("/tick")
    async def tick(request: Request, background: BackgroundTasks) -> Response:
        # Cloud Scheduler posts here with an OIDC token and no webhook
        # headers, so the signature gate must not run — it would 401 every
        # tick. A customer whose configured webhook URL happens to end in
        # /tick is a real collision: Anthropic posts to whatever path the
        # admin set and no suffix is reserved. A request carrying
        # webhook-id is therefore the delivery it claims to be.
        if "webhook-id" in request.headers:
            return await handle_frame(request, background)
        if store is None:
            return JSONResponse({"flushed": 0})
        counters: dict[str, int] = {}
        try:
            # Readers first: a `complete` here can make a record ready, and
            # it should go out on this tick rather than the next.
            counters = await run_readers(store=store, config=config, http=http())
        except Exception:
            # Never a failed tick. The scheduler would retry it, re-running
            # a reader that already moved its watermark.
            log.exception("tick: the reader pass failed; flushing anyway")
        flushed = await flush_due(store, config=config, client=http())
        return JSONResponse({"flushed": flushed, **counters})
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest -v && uv run ty check`. Expected: 4 passed in `test_readers.py`, `test_main.py` up by 2, and the whole subproject green.

- [ ] **Step 5: Run every subproject the chunk touched**

```bash
cd anthropic && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q
cd ../shared && uv run ruff check . && uv run ty check && uv run pytest -q
cd ../vertex && uv run ruff check . && uv run ty check && uv run pytest -q
```

Expected: `anthropic` green with the ~70 tests this chunk adds, `370 passed` in `shared` and `147 passed` in `vertex` — the counts Task 7.4 moved.

- [ ] **Step 6: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/compliance/readers.py \
        anthropic/src/slashid_anthropic_forwarder/main.py \
        anthropic/tests/test_readers.py anthropic/tests/test_main.py
git commit -m "feat(anthropic): run both readers on the tick, ahead of the flush"
```

---

## Chunk 8: Deployment, release, docs

Everything the customer touches. The receiver's `Config` still carries only the hook's knobs, so the first three tasks close it against the design's Configuration table and settle the two conflicts the design calls out by name: `_check_signing` refuses to start without a signing secret, which makes a compliance-only deployment impossible, and nothing asserts that *some* credential is present. The third conflict is one the store inherited — the design says the TTL policy "keys off `tombstoned_at`", but Firestore TTL deletes a document as soon as the field's instant is in the past, so keying it literally on the moment of tombstoning gives the tombstone no guaranteed lifetime at all; the store therefore writes the expiry instant itself and the policy keys on that. Then the Terraform module: one Cloud Run v2 service with two routes, a Cloud Scheduler job firing `POST /tick` under an OIDC token, the named Firestore database with the composite index `due` needs and the TTL policy, three secrets, a least-privilege service account, and an Artifact Registry remote repository proxying `ghcr.io` because Cloud Run pulls from Artifact Registry and nowhere else. **Nothing in the module serializes ticks** — Cloud Run hands a second concurrent request to a second instance, and `max_instance_request_concurrency` is about in-instance load, not mutual exclusion; the tick's Firestore lease is the only thing that makes overlap safe, and no comment here may claim otherwise. Finally the release workflow, the two READMEs, and the full toolchain across `anthropic`, `shared` and `vertex` — `vertex` because Chunk 5 promoted `CheckpointStore` out of it — before a PR that is **created and then left alone**.

### Task 8.1: the store, compliance and enrichment knobs

`config.py` has fourteen fields; the design's Configuration table names twenty-six. The missing twelve are every knob the pending store, the compliance readers and attachment enrichment read, plus one the table does not list and the Deployment section requires: `SLASHID_TICK_INTERVAL_SECONDS`. The cadence lives in Terraform as a cron string, and a cron string is not a number the process can compare — but `SLASHID_TOMBSTONE_TTL_SECONDS` must exceed `JOIN_WAIT` + `POLL_LAG` + one tick, and Task 8.2 asserts that at startup. So the interval is a declared input, and the module derives the cron from it rather than the other way round.

`gcp_project_id` is required rather than optional, following `vertex/src/slashid_vertex_forwarder/config.py:21`. The design says "required with the store", and both capabilities write to the store: there is no configuration of this service that runs without Firestore.

One Terraform-shaped detail comes with it. The module always sets every env var it manages, passing `""` for the ones a deployment leaves out, and pydantic-settings does not treat `""` as unset for a `str | None` field — so `SLASHID_COMPLIANCE_KEY=""` would switch the readers *on* with an empty key. One `mode="before"` validator over the five optional strings fixes it for all of them.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/config.py`
- Test: `anthropic/tests/test_config.py`

- [ ] **Step 1: Write the failing tests** — in `anthropic/tests/test_config.py`, first add the new required field to the `_env` helper so the existing five tests keep building a valid `Config`:

```python
def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "SLASHID_ENDPOINT": "https://api.slashid.com",
        "SLASHID_PUSH_TOKEN": "token",
        "SLASHID_HOOK_SIGNING_SECRET": "whsec_AAA",
        "SLASHID_GCP_PROJECT_ID": "proj",
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
```

then append:

```python
def test_store_defaults_match_the_design_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.gcp_project_id == "proj"
    # vertex names its own database rather than using ``(default)``; so does this.
    assert cfg.firestore_database == "slashid-anthropic"
    assert cfg.pending_collection == "anthropic_pending"
    assert cfg.join_wait_seconds == 3600
    assert cfg.tombstone_ttl_seconds == 7200
    assert cfg.max_flushes_per_tick == 500
    assert cfg.tick_interval_seconds == 300
    assert cfg.join_wait == timedelta(hours=1)
    assert cfg.tombstone_ttl == timedelta(hours=2)


def test_compliance_and_enrichment_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.compliance_key is None
    assert cfg.organization_uuid is None
    assert cfg.poll_lag_seconds == 120
    assert cfg.max_sessions_per_tick == 200
    assert cfg.attachment_hashing == "md5"
    assert cfg.max_attachment_fetch_bytes == 10 * 1024 * 1024


def test_gcp_project_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both capabilities write to the pending store, so there is no
    configuration of this service that runs without Firestore."""
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_GCP_PROJECT_ID")
    with pytest.raises(ValidationError):
        Config()


def test_empty_strings_from_terraform_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module always sets every env var it manages, ``""`` for the ones a
    deployment leaves out. An empty compliance key must not switch the
    readers on."""
    _env(
        monkeypatch,
        SLASHID_POLICY_URL="",
        SLASHID_COMPLIANCE_KEY="",
        SLASHID_ORGANIZATION_UUID="",
        SLASHID_CAPTURE_BUCKET="",
        SLASHID_CAPTURE_DENY_MARKER="",
    )
    cfg = Config()
    assert cfg.policy_url is None
    assert cfg.compliance_key is None
    assert cfg.organization_uuid is None
    assert cfg.capture_bucket is None
    assert cfg.capture_deny_marker is None


def test_attachment_hashing_must_be_md5_or_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_ATTACHMENT_HASHING="sha256")
    with pytest.raises(ValidationError):
        Config()
```

and extend the imports at the top of the file with `from datetime import timedelta`.

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_config.py -q`. Expected: the five new tests fail with `AttributeError: 'Config' object has no attribute 'gcp_project_id'` (and `test_gcp_project_id_is_required` fails with `DID NOT RAISE`). The pre-existing five still pass.

- [ ] **Step 3: Implement** — in `config.py`, extend the imports to `from datetime import timedelta`, `from pydantic import Field, field_validator, model_validator`, and append the fields after `capture_deny_marker`:

```python
    # --- Pending store ----------------------------------------------------
    # The project holding Firestore. Required, not optional: both
    # capabilities write to the pending store, so no configuration of this
    # service runs without it.
    gcp_project_id: str = Field(..., min_length=1)
    # Named database. ``vertex/`` names its own ``slashid-vertex`` rather
    # than using ``(default)``; this follows that.
    firestore_database: str = Field(default="slashid-anthropic", min_length=1)
    # Collection holding pending records and their tombstones.
    pending_collection: str = Field(default="anthropic_pending", min_length=1)
    # Deadline before an unsettled record is pushed as it stands. A push is
    # a commitment the terminal will not top up, so this sits beyond normal
    # reader lag rather than being trimmed for latency.
    join_wait_seconds: int = 3_600
    # How long a pushed record's tombstone suppresses a late reader's
    # duplicate. Must exceed JOIN_WAIT + POLL_LAG + one tick — asserted in
    # the validator below, since a reader arriving after its own tombstone
    # expired re-emits.
    tombstone_ttl_seconds: int = 7_200
    # Bounds ``due`` so one tick cannot stall behind a backlog.
    max_flushes_per_tick: int = 500
    # The Cloud Scheduler cadence, declared here as a number: the cron
    # string in Terraform is not something this process can compare against
    # ``tombstone_ttl_seconds``. The module derives the cron from it.
    tick_interval_seconds: int = 300

    # --- Compliance readers -----------------------------------------------
    # sk-ant-api01-…. Setting it enables the readers.
    compliance_key: str | None = None
    # Required with the key: it can read every linked organization, so the
    # readers filter to this one.
    organization_uuid: str | None = None
    # How far behind now the ``updated_at.gte`` bound sits.
    poll_lag_seconds: int = 120
    # Bounds a tick against the 600 rpm shared with the sync adapter.
    max_sessions_per_tick: int = 200

    # --- Attachment enrichment --------------------------------------------
    # ``md5`` takes the listing's digest and makes no request; ``full``
    # downloads and digests with every algorithm.
    attachment_hashing: Literal["md5", "full"] = "md5"
    # Under ``full``, the largest attachment worth downloading. Decided from
    # the listing's ``size_bytes`` before any fetch; an oversized file falls
    # back to the listing's md5 rather than to no digest.
    max_attachment_fetch_bytes: int = 10 * 1024 * 1024

    @property
    def join_wait(self) -> timedelta:
        return timedelta(seconds=self.join_wait_seconds)

    @property
    def tombstone_ttl(self) -> timedelta:
        return timedelta(seconds=self.tombstone_ttl_seconds)

    @field_validator(
        "policy_url",
        "compliance_key",
        "organization_uuid",
        "capture_bucket",
        "capture_deny_marker",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # Terraform sets every env var it manages, ``""`` where a deployment
        # left it out, and pydantic-settings does not treat ``""`` as unset.
        return v or None
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_config.py -q && uv run ty check`. Expected: 10 passed.

- [ ] **Step 5: Give every other Config factory the required field** — `grep -rn "Config(" anthropic/tests` and add `gcp_project_id` (keyword form) or `SLASHID_GCP_PROJECT_ID` (env form) to each base dict, exactly as Step 1 did for `_env`. `anthropic/tests/test_main.py:40` is the one that exists before this chunk:

```python
def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.com",
        "push_token": "tok",
        "hook_signing_secret": SECRET,
        "gcp_project_id": "proj",
    }
```

Then `cd anthropic && uv run pytest -q`. Expected: green, with no test failing on a missing `gcp_project_id`.

- [ ] **Step 6: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/config.py anthropic/tests
git commit -m "feat(anthropic): store, compliance and enrichment config knobs"
```

### Task 8.2: capabilities, at least one credential, and the tombstone inequality

The two conflicts the design names, plus the startup assertion the Deployment section requires.

`_check_signing` raises when no signing secret is set and `HOOK_ALLOW_UNSIGNED` is off. That was right when the hook was the only capability; it now refuses to start the compliance-only deployment the design calls for — no public endpoint, no certificate, no minimum instance, a scheduler and two secrets. The rule becomes: the signing secret enables the hook, the compliance key enables the readers, **at least one of the two must be present**, and the secret is required only when the hook is the capability in use.

The inequality is the other half of Task 8.1's `tick_interval_seconds`. `tombstone_ttl_seconds` must exceed `join_wait_seconds + poll_lag_seconds + tick_interval_seconds`, or a reader arrives after its own tombstone expired and re-emits — harmless in the graph, noisy in detections, since the terminal's dedup window is 72 hours but the tombstone is what suppresses the duplicate *before* the push. Note what the defaults leave: 7200 − 3600 − 120 = 3480 s of tick interval, so the hook-only "slow tick" cannot be hourly at the default `join_wait`.

`test_signing_secret_required_unless_unsigned_allowed` keeps passing unchanged — with no compliance key in `_env`, deleting the signing secret still leaves zero capabilities — but it now fails for the capability reason, so its assertion on the message, if any, is the thing to check.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/config.py`
- Test: `anthropic/tests/test_config.py`

- [ ] **Step 1: Write the failing tests** — append:

```python
def test_compliance_only_starts_without_a_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conflict the design names: the old validator made the
    compliance-only deployment impossible to start."""
    _env(monkeypatch, SLASHID_COMPLIANCE_KEY="sk-ant-api01-x", SLASHID_ORGANIZATION_UUID="org-1")
    monkeypatch.delenv("SLASHID_HOOK_SIGNING_SECRET")
    cfg = Config()
    assert cfg.hook_enabled is False
    assert cfg.compliance_enabled is True


def test_hook_only_needs_no_compliance_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.hook_enabled is True
    assert cfg.compliance_enabled is False


def test_no_credential_at_all_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_HOOK_SIGNING_SECRET")
    with pytest.raises(ValidationError, match="no capability configured"):
        Config()


def test_compliance_key_requires_an_organization_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key reads every linked organization; the readers filter to one."""
    _env(monkeypatch, SLASHID_COMPLIANCE_KEY="sk-ant-api01-x")
    with pytest.raises(ValidationError, match="SLASHID_ORGANIZATION_UUID"):
        Config()


def test_tombstone_ttl_must_outlive_join_wait_poll_lag_and_one_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3600 + 120 + 3600 > 7200: an hourly tick at the default join wait is
    exactly the configuration the assertion exists to refuse."""
    _env(monkeypatch, SLASHID_TICK_INTERVAL_SECONDS="3600")
    with pytest.raises(ValidationError, match="SLASHID_TOMBSTONE_TTL_SECONDS"):
        Config()
    _env(monkeypatch, SLASHID_TICK_INTERVAL_SECONDS="3600", SLASHID_TOMBSTONE_TTL_SECONDS="10800")
    assert Config().tombstone_ttl_seconds == 10800


def test_unsigned_hook_still_counts_as_a_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_HOOK_ALLOW_UNSIGNED="true")
    monkeypatch.delenv("SLASHID_HOOK_SIGNING_SECRET")
    assert Config().hook_enabled is True
```

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_config.py -q`. Expected: `test_compliance_only_starts_without_a_signing_secret` fails with the old `SLASHID_HOOK_SIGNING_SECRET is required unless HOOK_ALLOW_UNSIGNED`, the rest with `AttributeError: … 'hook_enabled'` or `DID NOT RAISE`.

- [ ] **Step 3: Implement** — replace `_check_signing` wholesale:

```python
    @property
    def hook_enabled(self) -> bool:
        """The signing secret enables the hook; ``HOOK_ALLOW_UNSIGNED`` is the
        escape hatch for an org that enabled hooks before secrets existed."""
        return bool(self.signing_secrets) or self.hook_allow_unsigned

    @property
    def compliance_enabled(self) -> bool:
        """The compliance key enables the two readers."""
        return bool(self.compliance_key)

    @model_validator(mode="after")
    def _check_capabilities(self) -> Config:
        # At least one credential, or there is nothing to run. The signing
        # secret is required only when the hook is the capability in use:
        # compliance-only needs none, and demanding one made that deployment
        # impossible to start.
        if not self.hook_enabled and not self.compliance_enabled:
            raise ValueError(
                "no capability configured: set SLASHID_HOOK_SIGNING_SECRET for the hook "
                "(or SLASHID_HOOK_ALLOW_UNSIGNED), SLASHID_COMPLIANCE_KEY for the readers"
            )
        if self.hook_allow_unsigned and self.policy_url:
            raise ValueError("HOOK_ALLOW_UNSIGNED cannot be combined with POLICY_URL")
        if self.compliance_enabled and not self.organization_uuid:
            raise ValueError("SLASHID_ORGANIZATION_UUID is required with SLASHID_COMPLIANCE_KEY")
        floor = self.join_wait_seconds + self.poll_lag_seconds + self.tick_interval_seconds
        if self.tombstone_ttl_seconds <= floor:
            raise ValueError(
                f"SLASHID_TOMBSTONE_TTL_SECONDS ({self.tombstone_ttl_seconds}) must exceed "
                f"JOIN_WAIT + POLL_LAG + one tick ({floor}): a reader arriving after its own "
                "tombstone expired re-emits the invocation"
            )
        return self
```

- [ ] **Step 4: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest tests/test_config.py -q && uv run ty check`. Expected: 16 passed, `test_signing_secret_required_unless_unsigned_allowed` among them.

- [ ] **Step 5: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder/config.py anthropic/tests/test_config.py
git commit -m "fix(anthropic): capabilities follow the credentials, not the signing secret"
```

### Task 8.3: the tombstone carries its own expiry instant

Firestore's TTL policy deletes a document once the timestamp in the nominated field is **in the past**. Keying it on `tombstoned_at` — the instant the record was retired — therefore asks Firestore to delete every tombstone as soon as it is written, and the only thing standing between that and a re-emitted invocation is Firestore's undocumented deletion lag. The design's wording ("the TTL policy keyed off `tombstoned_at`") describes the intent; the field the policy can actually key on is the expiry instant, which is `tombstoned_at + SLASHID_TOMBSTONE_TTL_SECONDS`. `tombstoned_at` stays exactly what it is and keeps its meaning — every tombstone check in `store.py` is `is not None` and reads no value — and a second field carries the deletion time.

**Files:**
- Modify: `anthropic/src/slashid_anthropic_forwarder/store.py`
- Test: `anthropic/tests/test_store.py`

- [ ] **Step 1: Write the failing tests** — append to `anthropic/tests/test_store.py`:

```python
TOMBSTONE_TTL = timedelta(hours=2)


async def test_a_tombstone_carries_its_own_expiry_instant() -> None:
    """The Firestore TTL policy deletes when the nominated field is in the
    past, so the field must hold the expiry, not the moment of tombstoning."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.PUSHED, now=NOW)
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["tombstoned_at"] == NOW
    assert stored["tombstone_expires_at"] == NOW + TOMBSTONE_TTL


async def test_a_superseded_tail_expires_on_the_same_clock() -> None:
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.retire(ADDRESS, Retirement.SUPERSEDED, now=NOW)
    stored = client.docs["anthropic_pending/" + ADDRESS][0]
    assert stored["tombstone_expires_at"] == NOW + TOMBSTONE_TTL


async def test_a_failed_push_writes_no_expiry() -> None:
    """A live record must never be visible to the TTL policy."""
    store, client = a_store(tombstone_ttl=TOMBSTONE_TTL)
    await store.upsert(ADDRESS, {"event": an_event()}, (), now=NOW)
    await store.claim(ADDRESS, LEASE, owner="sweep", now=PAST_DEADLINE)
    await store.retire(ADDRESS, Retirement.FAILED, now=PAST_DEADLINE)
    assert "tombstone_expires_at" not in client.docs["anthropic_pending/" + ADDRESS][0]
```

and widen the `a_store` helper so it forwards the new keyword (it already forwards `join_wait`).

- [ ] **Step 2: Run to verify they fail** — `cd anthropic && uv run pytest tests/test_store.py -q`. Expected: `TypeError: … unexpected keyword argument 'tombstone_ttl'`.

- [ ] **Step 3: Implement** — in `FirestorePendingStore.__init__`, add the parameter beside `join_wait`:

```python
        tombstone_ttl: timedelta = timedelta(hours=2),
```
```python
        self._tombstone_ttl = tombstone_ttl
```

and in `retire`, the non-`FAILED` branch:

```python
            # Push, then retire: a crash between them re-pushes an event the
            # terminal dedups, where the reverse order loses it outright.
            #
            # ``tombstone_expires_at`` is what the TTL policy keys on.
            # Firestore deletes a document once the nominated field is in the
            # past, so the field holds the expiry instant rather than the
            # moment of tombstoning — keying it on ``tombstoned_at`` would ask
            # for the tombstone to be deleted the moment it is written. No
            # live record ever carries the field, so the policy cannot reach
            # one.
            await self._ref(address).set(
                {
                    "tombstoned_at": now,
                    "tombstone_expires_at": now + self._tombstone_ttl,
                    "claim_owner": None,
                    "claim_expires_at": None,
                },
                merge=True,
            )
```

Update the module docstring's "the TTL policy keyed off ``tombstoned_at``" to name `tombstone_expires_at`, and `due`'s docstring if it repeats it.

- [ ] **Step 4: Wire the config through to the store** — `grep -rn "FirestorePendingStore(" anthropic/src`. At that construction site, pass `tombstone_ttl=config.tombstone_ttl` alongside the existing `join_wait=`, `collection=config.pending_collection` and `client` built against `config.gcp_project_id` / `config.firestore_database`.

- [ ] **Step 5: Run to verify they pass** — `cd anthropic && uv run ruff format . && uv run ruff check --fix . && uv run pytest -q && uv run ty check`. Expected: green, three tests more than before.

- [ ] **Step 6: Commit**

```bash
git add anthropic/src/slashid_anthropic_forwarder anthropic/tests/test_store.py
git commit -m "fix(anthropic): tombstones carry the expiry instant the ttl policy keys on"
```

### Task 8.4: the Terraform module

One module, mirroring `vertex/deploy/terraform/` in shape and in naming convention (`slashid_anthropic_` with underscores for Secret Manager and Firestore, `slashid-anthropic-` with hyphens for Cloud Run, Scheduler and service accounts). It provisions both supported topologies from one set of variables: supply `hook_signing_secret` and you get a public service with `min_instance_count = 1`; supply `compliance_key` and you get the scheduler and the readers; supply both and you get the joined deployment.

**Files:**
- Create: `anthropic/deploy/terraform/{versions,variables,main,registry,secrets,firestore,service,scheduler,iam,outputs}.tf`
- Create: `anthropic/deploy/terraform/README.md`

- [ ] **Step 1: `versions.tf` and `variables.tf`**

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

```hcl
# --- Required customer inputs ---------------------------------------------

variable "project_id" {
  description = "GCP project that hosts the receiver, its Firestore database and its secrets."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud Scheduler, Artifact Registry and Firestore."
  type        = string
  default     = "us-central1"
}

variable "slashid_endpoint" {
  description = "SlashID base URL for the NHI events endpoint (e.g. https://api.slashid.com)."
  type        = string
}

variable "slashid_push_token" {
  description = "Push token of the SlashID ``anthropic`` connection. Sensitive — stored in Secret Manager. One deployment shares one token by construction; splitting the hook and the readers across deployments loses the join, and splitting them across connections double-counts every invocation both halves saw."
  type        = string
  sensitive   = true
}

variable "release_version" {
  description = "Receiver release tag (e.g. \"anthropic-v0.1.0\"). The image tag is the bare version."
  type        = string
}

# --- Capabilities ----------------------------------------------------------
#
# At least one of ``hook_signing_secret`` and ``compliance_key`` must be
# set: they are what the service's own startup check calls a capability,
# and with neither it refuses to start.

variable "hook_signing_secret" {
  description = "whsec_… generated when the Inference hooks endpoint is configured. Comma-join any number to accept them all during a rotation. Empty disables the hook. Sensitive."
  type        = string
  default     = ""
  sensitive   = true
}

variable "hook_allow_unsigned" {
  description = "Accept unsigned frames. Escape hatch for an organization that enabled hooks before signing secrets were required; cannot be combined with policy_url."
  type        = bool
  default     = false
}

variable "compliance_key" {
  description = "Compliance Access Key (sk-ant-api01-…) with read:compliance_activities and read:compliance_user_data. Empty disables the readers. Sensitive."
  type        = string
  default     = ""
  sensitive   = true
}

variable "organization_uuid" {
  description = "Required with compliance_key: the key can read every linked organization, so the readers filter to this one."
  type        = string
  default     = ""
}

# --- Verdict knobs ---------------------------------------------------------

variable "policy_url" {
  description = "The ng-evangelion receiver's /ai-access/<id>. Empty skips the policy check."
  type        = string
  default     = ""
}

variable "preflight_enabled" {
  description = "Call {slashid_endpoint}/ip/nhi/ai/preflight for the content check. Keep off until that endpoint is deployed: against a 404 every frame takes the fail-mode path."
  type        = bool
  default     = false
}

variable "verdict_fail_mode" {
  description = "allow or deny when a check fails or answers unverified. Distinct from Anthropic's own failure handling, which covers the case where this service does not answer at all."
  type        = string
  default     = "allow"

  validation {
    condition     = contains(["allow", "deny"], var.verdict_fail_mode)
    error_message = "verdict_fail_mode must be \"allow\" or \"deny\"."
  }
}

variable "shadow_mode" {
  description = "Our own shadow mode, named after claude.ai's and independent of it: when either is on, nothing is blocked. True by default so a fresh deployment observes before it enforces."
  type        = bool
  default     = true
}

variable "verdict_budget_ms" {
  description = "Both checks run concurrently under this budget, inside Anthropic's configured verdict timeout."
  type        = number
  default     = 3500
}

variable "push_budget_ms" {
  description = "Bounds the background push so a hung sink cannot pin an instance. Never delays a verdict."
  type        = number
  default     = 2000
}

# --- Pending store ---------------------------------------------------------

variable "join_wait_seconds" {
  description = "Deadline before an unsettled record is pushed as it stands. A push is a commitment the terminal's dedup will not top up, so this sits beyond normal reader lag rather than being trimmed for latency."
  type        = number
  default     = 3600
}

variable "tombstone_ttl_seconds" {
  description = <<-EOT
    How long a pushed record's tombstone suppresses a late reader's
    duplicate. Must exceed join_wait_seconds + poll_lag_seconds +
    tick_interval_seconds — the service asserts the same inequality at
    startup and refuses to run if it fails, so a violation here is a failed
    revision rather than a silent re-emission.

    The defaults leave 3480 s of tick interval (7200 − 3600 − 120), so a
    hook-only deployment cannot run an hourly tick without raising this.
  EOT
  type        = number
  default     = 7200
}

variable "max_flushes_per_tick" {
  description = "Bounds the ``due`` query so one tick cannot stall behind a backlog."
  type        = number
  default     = 500
}

variable "poll_lag_seconds" {
  description = "How far behind now the readers' updated_at.gte bound sits."
  type        = number
  default     = 120
}

variable "max_sessions_per_tick" {
  description = "Bounds a tick against the 600 rpm the Compliance API shares with the sync adapter."
  type        = number
  default     = 200
}

variable "attachment_hashing" {
  description = "md5 takes the digest the file listing already carries and makes no request; full downloads the bytes and digests them with every algorithm."
  type        = string
  default     = "md5"

  validation {
    condition     = contains(["md5", "full"], var.attachment_hashing)
    error_message = "attachment_hashing must be \"md5\" or \"full\"."
  }
}

variable "max_attachment_fetch_bytes" {
  description = "Under full hashing, the largest attachment worth downloading. Decided from the listing's size_bytes before any fetch; an oversized file keeps the listing's md5 rather than losing its digest."
  type        = number
  default     = 10485760
}

# --- Tick cadence ----------------------------------------------------------

variable "tick_interval_seconds" {
  description = <<-EOT
    How often Cloud Scheduler fires ``POST /tick``. The schedule is derived
    from this number rather than taken as a cron string, because the service
    checks ``tombstone_ttl_seconds`` against it and cannot compare a cron
    expression to a number.

    Cloud Scheduler has no sub-minute granularity, so 60 is the tightest
    cadence.
  EOT
  type        = number
  default     = 300

  validation {
    condition = (
      var.tick_interval_seconds >= 60 &&
      var.tick_interval_seconds % 60 == 0 &&
      (
        var.tick_interval_seconds <= 3600
        ? 3600 % var.tick_interval_seconds == 0
        : (var.tick_interval_seconds % 3600 == 0 && 86400 % var.tick_interval_seconds == 0)
      )
    )
    error_message = "tick_interval_seconds must divide an hour (60, 120, 180, 300, 600, 900, 1200, 1800, 3600) or be a whole number of hours dividing a day (7200, 10800, 14400, 21600, 43200, 86400): the unix-cron schedule is derived from it as a step."
  }
}

variable "tick_attempt_deadline_seconds" {
  description = "How long Cloud Scheduler waits for a tick before giving up. Must not exceed service_timeout_seconds."
  type        = number
  default     = 540
}

# --- Service shape ---------------------------------------------------------

variable "min_instances" {
  description = "Applies only when the hook is enabled: a cold start inside Anthropic's verdict timeout risks a webhook failure, and enough of those trip its circuit breaker. A compliance-only deployment pins this to 0 regardless."
  type        = number
  default     = 1
}

variable "max_instances" {
  type    = number
  default = 10
}

variable "service_timeout_seconds" {
  description = "Cloud Run request timeout. Sized for the tick, not the hook — a hook verdict is bounded by verdict_budget_ms."
  type        = number
  default     = 600
}

variable "max_body_bytes" {
  description = "Request body cap. Cloud Run caps HTTP/1 bodies at 32 MiB, under the protocol's 64 MiB ceiling; observed frames peak at 1.86 MB."
  type        = number
  default     = 33554432
}

variable "include_raw_content" {
  type    = bool
  default = false
}

variable "max_content_size" {
  type    = number
  default = 100000
}

variable "log_level" {
  type    = string
  default = "INFO"
}

# --- Image + registry ------------------------------------------------------

variable "image" {
  description = "Full image reference. Overrides the one derived from release_version; use for a locally built image."
  type        = string
  default     = ""
}

variable "ghcr_username" {
  description = "GitHub username whose token can read the image package. Required while the repository — and therefore its packages — is private."
  type        = string
  default     = ""
}

variable "ghcr_token" {
  description = "GitHub token (classic, scope read:packages) paired with ghcr_username. Sensitive — stored in Secret Manager for the registry's service agent to read."
  type        = string
  default     = ""
  sensitive   = true
}

# --- Naming overrides ------------------------------------------------------

variable "create_firestore_database" {
  description = "Provision the named Firestore database. False reuses an existing one — Firestore databases are hard to fully delete, so a destroy/re-apply cycle usually leaves one behind."
  type        = bool
  default     = true
}

variable "firestore_database" {
  description = "Named Firestore database, kept isolated from the project's (default) database as vertex/ does."
  type        = string
  default     = "slashid-anthropic"
}

variable "pending_collection" {
  description = "Firestore collection holding pending records and their tombstones. The composite index and the TTL policy are provisioned against it."
  type        = string
  default     = "anthropic_pending"
}

variable "service_name" {
  type    = string
  default = "slashid-anthropic-forwarder"
}

variable "service_account_id" {
  type    = string
  default = "slashid-anthropic-sa"
}

variable "scheduler_service_account_id" {
  description = "Service account Cloud Scheduler mints its OIDC token as. Separate from the runtime SA: its only privilege is invoking this one service."
  type        = string
  default     = "slashid-anthropic-tick-sa"
}

variable "scheduler_name" {
  type    = string
  default = "slashid-anthropic-tick"
}

variable "registry_repository_id" {
  description = "Artifact Registry remote repository proxying ghcr.io. Cloud Run pulls from Artifact Registry and nowhere else."
  type        = string
  default     = "slashid-ghcr"
}

variable "hook_path" {
  description = "Path component of the hook URL. Any path works — the receiver answers POST on all of them; this one says what it is."
  type        = string
  default     = "/hooks/anthropic"
}
```

- [ ] **Step 2: `main.tf`** — provider, capability locals, the derived schedule, API enablement:

```hcl
# Provider + shared locals.
#
# The module runs against the customer's own GCP project — no SlashID
# infrastructure sits between the customer and their data.
#
# One service, two routes. ``POST /{path}`` is the Inference hooks
# endpoint; ``POST /tick`` drives the compliance readers and the deadline
# flush. Which of the two does anything is decided by the credentials
# below, not by a mode flag: hook-only, compliance-only and both are
# configurations of one image.

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  hook_enabled       = var.hook_signing_secret != "" || var.hook_allow_unsigned
  compliance_enabled = var.compliance_key != ""

  # The release workflow tags the image with the bare version from
  # anthropic/pyproject.toml: ``anthropic-v0.1.0`` → ``:0.1.0``.
  version_short = trimprefix(var.release_version, "anthropic-v")
  image = var.image != "" ? var.image : join("", [
    "${var.region}-docker.pkg.dev/${var.project_id}/${var.registry_repository_id}",
    "/slashid/slashid-anthropic-forwarder:${local.version_short}",
  ])

  # The cadence is an input as a NUMBER (the service compares it against
  # the tombstone TTL at startup); the cron string is derived from it.
  # ``tick_interval_seconds`` is validated to divide an hour or a day, so
  # neither branch can produce a fractional step.
  tick_minutes  = var.tick_interval_seconds / 60
  tick_schedule = local.tick_minutes < 60 ? "*/${local.tick_minutes} * * * *" : "0 */${local.tick_minutes / 60} * * *"
}

resource "google_project_service" "required" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  service = each.value
  # Leaving APIs enabled after ``terraform destroy`` is safer in a shared
  # project, and they are free once enabled.
  disable_on_destroy         = false
  disable_dependent_services = false
}

data "google_project" "this" {}
```

- [ ] **Step 3: `registry.tf` and `secrets.tf`**

```hcl
# Cloud Run pulls images from Artifact Registry only, so a REMOTE
# repository proxies ghcr.io, where the release workflow publishes.

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

    # The API validates the upstream credential at create time, before the
    # service agent's read grant below has necessarily propagated.
    disable_upstream_validation = true
  }

  depends_on = [
    google_project_service.required,
    google_secret_manager_secret_iam_member.registry_reads_ghcr_token,
  ]
}

# The registry's own service agent reads the upstream token — not the
# runtime service account.
resource "google_secret_manager_secret_iam_member" "registry_reads_ghcr_token" {
  count     = var.ghcr_username == "" ? 0 : 1
  secret_id = google_secret_manager_secret.ghcr_token[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:service-${data.google_project.this.number}@gcp-sa-artifactregistry.iam.gserviceaccount.com"

  depends_on = [google_project_service.required]
}
```

```hcl
# Three secrets, each created only when the capability that needs it is
# configured. The push token is unconditional: both capabilities push.

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
  count     = var.hook_signing_secret == "" ? 0 : 1
  secret_id = "slashid_anthropic_signing_secret"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "signing_secret" {
  count       = var.hook_signing_secret == "" ? 0 : 1
  secret      = google_secret_manager_secret.signing_secret[0].id
  secret_data = var.hook_signing_secret
}

resource "google_secret_manager_secret" "compliance_key" {
  count     = local.compliance_enabled ? 1 : 0
  secret_id = "slashid_anthropic_compliance_key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "compliance_key" {
  count       = local.compliance_enabled ? 1 : 0
  secret      = google_secret_manager_secret.compliance_key[0].id
  secret_data = var.compliance_key
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

- [ ] **Step 4: `firestore.tf`** — the named database, the composite index `due` needs, and the TTL policy:

```hcl
# Firestore Native-mode, in a NAMED database (``slashid-anthropic``)
# rather than the project's ``(default)`` — the same isolation
# ``vertex/`` takes for its own ``slashid-vertex``.
#
# Two collections live in it: the pending records (below) and the
# compliance readers' checkpoint documents, which are created on the
# first save and need no resource here.

resource "google_firestore_database" "pending" {
  count = var.create_firestore_database ? 1 : 0

  project     = var.project_id
  name        = var.firestore_database
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # Firestore databases cannot be undeleted.
  deletion_policy = "ABANDON"

  depends_on = [google_project_service.required]
}

# The deadline sweep runs
#
#   where(tombstoned_at == None).where(next_attempt_at <= now)
#     .order_by(next_attempt_at).limit(max_flushes_per_tick)
#
# — an equality plus an inequality on a second field, which Firestore
# serves only from a composite index. Without it the query fails at
# runtime with FAILED_PRECONDITION and no record is ever flushed.
#
# ``next_attempt_at`` folds the deadline, the claim lease and the retry
# backoff into one field, which is why one inequality is enough and this
# index has two terms rather than three.
resource "google_firestore_index" "due" {
  project     = var.project_id
  database    = var.firestore_database
  collection  = var.pending_collection
  query_scope = "COLLECTION"

  fields {
    field_path = "tombstoned_at"
    order      = "ASCENDING"
  }

  fields {
    field_path = "next_attempt_at"
    order      = "ASCENDING"
  }

  depends_on = [google_firestore_database.pending]
}

# TTL policy. Firestore deletes a document once the nominated field's
# timestamp is in the PAST, and offers no duration of its own — so the
# field holds the expiry instant the store computed
# (``tombstoned_at + SLASHID_TOMBSTONE_TTL_SECONDS``), never the moment
# of tombstoning. Keying it on ``tombstoned_at`` would ask Firestore to
# delete each tombstone the moment it was written, leaving a late reader
# free to re-emit an invocation that was already pushed.
#
# A live record never carries this field, so the policy cannot reach one.
# That matters more than the duplicate does: expiring by creation time —
# the obvious implementation under a 7200 s default — would delete a
# record that had been failing to push for two hours before it was ever
# emitted, which is precisely the loss the store exists to prevent.
#
# ``index_config {}`` clears the single-field indexes on the TTL field;
# nothing queries it and Firestore recommends the exemption.
resource "google_firestore_field" "tombstone_ttl" {
  project    = var.project_id
  database   = var.firestore_database
  collection = var.pending_collection
  field      = "tombstone_expires_at"

  ttl_config {}

  index_config {}

  depends_on = [google_firestore_database.pending]
}
```

- [ ] **Step 5: `service.tf`** — the Cloud Run v2 service:

```hcl
# One service, two routes.
#
# ``max_instance_request_concurrency`` bounds how many requests share an
# instance. It does NOT serialize ticks: a second concurrent POST /tick
# is served by a second instance, and Cloud Scheduler's own retry does
# not suppress an overlap either. Overlapping ticks are the steady state
# under load, and the Firestore lease the tick takes before doing any
# work is the only thing that makes them safe. Do not "fix" overlap by
# lowering this value.

resource "google_cloud_run_v2_service" "receiver" {
  name     = var.service_name
  location = var.region

  # A hook deployment must be reachable from Anthropic's network. A
  # compliance-only one needs no public endpoint and no certificate:
  # Cloud Scheduler in the same project counts as internal traffic.
  ingress = local.hook_enabled ? "INGRESS_TRAFFIC_ALL" : "INGRESS_TRAFFIC_INTERNAL_ONLY"

  # Provider 6 defaults this to true, which makes ``terraform destroy``
  # fail until it is flipped. The durable state is in Firestore, which
  # this module abandons rather than deletes.
  deletion_protection = false

  template {
    service_account = google_service_account.receiver.email
    timeout         = "${var.service_timeout_seconds}s"

    max_instance_request_concurrency = 20

    scaling {
      # Only the hook needs a warm instance: a cold start inside
      # Anthropic's verdict timeout risks a webhook failure, and enough
      # of those trip its circuit breaker. A scheduled reader can wait.
      min_instance_count = local.hook_enabled ? var.min_instances : 0
      max_instance_count = var.max_instances
    }

    containers {
      image = local.image

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        # The push runs after the response has gone out, so CPU stays
        # allocated between requests.
        cpu_idle = false
      }

      env {
        name  = "SLASHID_ENDPOINT"
        value = var.slashid_endpoint
      }
      env {
        name  = "SLASHID_GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "SLASHID_FIRESTORE_DATABASE"
        value = var.firestore_database
      }
      env {
        name  = "SLASHID_PENDING_COLLECTION"
        value = var.pending_collection
      }
      env {
        name  = "SLASHID_JOIN_WAIT_SECONDS"
        value = tostring(var.join_wait_seconds)
      }
      env {
        name  = "SLASHID_TOMBSTONE_TTL_SECONDS"
        value = tostring(var.tombstone_ttl_seconds)
      }
      env {
        name  = "SLASHID_MAX_FLUSHES_PER_TICK"
        value = tostring(var.max_flushes_per_tick)
      }
      env {
        name  = "SLASHID_TICK_INTERVAL_SECONDS"
        value = tostring(var.tick_interval_seconds)
      }
      env {
        name  = "SLASHID_POLL_LAG_SECONDS"
        value = tostring(var.poll_lag_seconds)
      }
      env {
        name  = "SLASHID_MAX_SESSIONS_PER_TICK"
        value = tostring(var.max_sessions_per_tick)
      }
      env {
        name  = "SLASHID_ORGANIZATION_UUID"
        value = var.organization_uuid
      }
      env {
        name  = "SLASHID_ATTACHMENT_HASHING"
        value = var.attachment_hashing
      }
      env {
        name  = "SLASHID_MAX_ATTACHMENT_FETCH_BYTES"
        value = tostring(var.max_attachment_fetch_bytes)
      }
      env {
        name  = "SLASHID_HOOK_ALLOW_UNSIGNED"
        value = tostring(var.hook_allow_unsigned)
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
        name  = "SLASHID_SHADOW_MODE"
        value = tostring(var.shadow_mode)
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
        name  = "SLASHID_MAX_BODY_BYTES"
        value = tostring(var.max_body_bytes)
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

      dynamic "env" {
        for_each = var.hook_signing_secret == "" ? [] : [1]
        content {
          name = "SLASHID_HOOK_SIGNING_SECRET"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.signing_secret[0].secret_id
              version = "latest"
            }
          }
        }
      }

      dynamic "env" {
        for_each = local.compliance_enabled ? [1] : []
        content {
          name = "SLASHID_COMPLIANCE_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.compliance_key[0].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    precondition {
      condition     = local.hook_enabled || local.compliance_enabled
      error_message = "Set hook_signing_secret (or hook_allow_unsigned) for the hook, compliance_key for the readers, or both. The service refuses to start with neither."
    }

    precondition {
      condition     = var.tombstone_ttl_seconds > var.join_wait_seconds + var.poll_lag_seconds + var.tick_interval_seconds
      error_message = "tombstone_ttl_seconds must exceed join_wait_seconds + poll_lag_seconds + tick_interval_seconds: a reader arriving after its own tombstone expired re-emits the invocation. The service asserts the same inequality at startup."
    }

    precondition {
      condition     = var.tick_attempt_deadline_seconds <= var.service_timeout_seconds
      error_message = "tick_attempt_deadline_seconds must not exceed service_timeout_seconds: Cloud Scheduler would give up on a tick Cloud Run is still running."
    }

    precondition {
      condition     = !(var.compliance_key != "" && var.organization_uuid == "")
      error_message = "organization_uuid is required with compliance_key: the key can read every linked organization, so the readers filter to one."
    }
  }

  depends_on = [
    google_secret_manager_secret_version.push_token,
    google_secret_manager_secret_iam_member.push_token,
    google_artifact_registry_repository.ghcr,
    google_project_iam_member.datastore_user,
  ]
}
```

- [ ] **Step 6: `scheduler.tf`**

```hcl
# Cloud Scheduler fires the tick: the compliance readers' pass and the
# deadline flush. It runs in every topology — a hook-only deployment
# still needs the flush, because the last round of a session has no
# successor frame to settle its record.
#
# OIDC, not a shared secret: the token is minted for a service account
# whose only privilege is invoking this service.
#
# ``retry_count = 0``. A retried tick does not resume the one that timed
# out and does not exclude it either; the next cron fire picks the work
# up from the store, and the tick's Firestore lease is what keeps two
# concurrent runs from pushing the same record twice.

resource "google_service_account" "scheduler" {
  account_id   = var.scheduler_service_account_id
  display_name = "SlashID Anthropic tick scheduler"
  description  = "Mints the OIDC token Cloud Scheduler presents to POST /tick."
}

resource "google_cloud_scheduler_job" "tick" {
  name             = var.scheduler_name
  region           = var.region
  schedule         = local.tick_schedule
  time_zone        = "UTC"
  attempt_deadline = "${var.tick_attempt_deadline_seconds}s"
  description      = "Drives the SlashID Anthropic readers and the deadline flush every ${var.tick_interval_seconds}s (UTC)."

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.receiver.uri}/tick"
    body        = base64encode("{}")

    headers = {
      "Content-Type" = "application/json"
    }

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience              = google_cloud_run_v2_service.receiver.uri
    }
  }

  depends_on = [
    google_project_service.required,
    google_cloud_run_v2_service_iam_member.scheduler_invoker,
  ]
}
```

- [ ] **Step 7: `iam.tf`**

```hcl
# The runtime service account. Every grant is the minimum one tick or
# one frame needs: Firestore for the pending store and the checkpoints,
# Secret Manager for the three secrets, Artifact Registry for the image.
# It holds no BigQuery, logging or storage role — this service reads
# nothing in the customer's project beyond its own state.

resource "google_service_account" "receiver" {
  account_id   = var.service_account_id
  display_name = "SlashID Anthropic forwarder"
  description  = "Runs the Cloud Run service that receives Inference hooks and polls the Compliance API."
}

resource "google_project_iam_member" "datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "push_token" {
  secret_id = google_secret_manager_secret.push_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "signing_secret" {
  count     = var.hook_signing_secret == "" ? 0 : 1
  secret_id = google_secret_manager_secret.signing_secret[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

resource "google_secret_manager_secret_iam_member" "compliance_key" {
  count     = local.compliance_enabled ? 1 : 0
  secret_id = google_secret_manager_secret.compliance_key[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.receiver.email}"
}

# Cloud Run pulls with its own service agent, which already holds
# roles/run.serviceAgent in-project; this grant matters only if the
# registry ever moves to another project. Kept so that move needs no
# IAM change.
resource "google_artifact_registry_repository_iam_member" "pull" {
  location   = google_artifact_registry_repository.ghcr.location
  repository = google_artifact_registry_repository.ghcr.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.receiver.email}"
}

# The scheduler's token is accepted because of this one grant.
resource "google_cloud_run_v2_service_iam_member" "scheduler_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_service.receiver.location
  name     = google_cloud_run_v2_service.receiver.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# Anthropic calls the hook URL unauthenticated — the Standard Webhooks
# signature is the authentication, and the receiver answers 401 without
# a valid one. Granted only when the hook is enabled, so a
# compliance-only deployment has no public surface at all.
#
# Note what it also exposes: ``POST /tick`` on the same service, which
# Cloud Run cannot scope per path. An unauthenticated tick does no more
# than the scheduled one — it takes the same lease, honours the same
# checkpoints and emits only what the next tick would have emitted — but
# it can be triggered, which is worth knowing before pointing a rate
# limiter at this service.
resource "google_cloud_run_v2_service_iam_member" "public" {
  count    = local.hook_enabled ? 1 : 0
  project  = var.project_id
  location = google_cloud_run_v2_service.receiver.location
  name     = google_cloud_run_v2_service.receiver.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
```

- [ ] **Step 8: `outputs.tf`**

```hcl
output "hook_url" {
  description = "Configure this as the Inference hooks endpoint in claude.ai. Empty when the hook is disabled."
  value       = local.hook_enabled ? "${google_cloud_run_v2_service.receiver.uri}${var.hook_path}" : ""
}

output "service_uri" {
  description = "Cloud Run service base URL. ``POST /tick`` under it is what Cloud Scheduler calls."
  value       = google_cloud_run_v2_service.receiver.uri
}

output "service_account_email" {
  description = "Service account the receiver runs as."
  value       = google_service_account.receiver.email
}

output "scheduler_service_account_email" {
  description = "Service account Cloud Scheduler mints its OIDC token as."
  value       = google_service_account.scheduler.email
}

output "tick_schedule" {
  description = "Unix-cron schedule derived from tick_interval_seconds."
  value       = local.tick_schedule
}

output "image" {
  description = "Image the service runs, resolved through the Artifact Registry proxy."
  value       = local.image
}

output "firestore_database" {
  description = "Named Firestore database holding the pending records and the reader checkpoints."
  value       = var.firestore_database
}

output "capabilities" {
  description = "Which halves this deployment runs."
  value = {
    hook       = local.hook_enabled
    compliance = local.compliance_enabled
  }
}
```

- [ ] **Step 9: Validate** — `cd anthropic/deploy/terraform && terraform fmt -recursive && terraform init -backend=false && terraform validate`. Expected: `Success! The configuration is valid.` CI runs `terraform fmt -check -recursive`, so commit the formatted files. If `fmt` rewrites anything, re-read the diff before committing — alignment only.

- [ ] **Step 10: Module README** — `anthropic/deploy/terraform/README.md`, in the shape of `vertex/deploy/terraform/README.md`: a `module` block with `source = "git::https://github.com/slashid/slashid-ai-forwarders.git//anthropic/deploy/terraform?ref=anthropic-v0.1.0"`, the three topologies as three example blocks (hook only, compliance only, both), the note that `ghcr_username`/`ghcr_token` are required while the package is private, the image tag convention (`anthropic-v0.1.0` → `:0.1.0`), the inequality between `tombstone_ttl_seconds` and the tick, and the claude.ai setup order: apply with no signing secret, copy `hook_url`, configure the endpoint in claude.ai, take the generated `whsec_…`, re-apply with `hook_signing_secret`, use claude.ai's **Test connection**, then its own staged rollout — shadow mode, a rollout percentage, role exclusions, enforcement — leaving `shadow_mode = true` here until the customer opts in.

- [ ] **Step 11: Commit**

```bash
git add anthropic/deploy/terraform
git commit -m "feat(anthropic): terraform module for cloud run, scheduler and firestore"
```

### Task 8.5: release workflow and CI

`release-vertex.yml` builds a source zip because Cloud Functions wants one. This one builds a container, because Cloud Run wants one — otherwise the same shape: the tag triggers, `anthropic/pyproject.toml` holds the version, and a mismatch fails the release rather than shipping a wrong-numbered artifact.

**Files:**
- Create: `.github/workflows/release-anthropic.yml`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write `release-anthropic.yml`**

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
        # The tag triggers the release; anthropic/pyproject.toml is the
        # source of truth for the number. Refuse on mismatch so a
        # fat-fingered tag cannot ship a wrong-numbered image.
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
          if [[ "${VERSION}" == *-* ]]; then
            echo "value=true" >> "$GITHUB_OUTPUT"
          else
            echo "value=false" >> "$GITHUB_OUTPUT"
          fi

      - uses: docker/build-push-action@v6
        with:
          # The build context is the repository root: the image needs
          # both shared/ and anthropic/ from the uv workspace.
          context: .
          file: anthropic/Dockerfile
          push: true
          # One immutable tag per release. Terraform pins by version, so
          # no floating tag exists for a pre-release to move under a
          # customer.
          tags: ghcr.io/slashid/slashid-anthropic-forwarder:${{ env.VERSION }}

      - uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
          prerelease: ${{ steps.prerelease.outputs.value }}
          body: |
            Image: `ghcr.io/slashid/slashid-anthropic-forwarder:${{ env.VERSION }}`

            Terraform: `source = "git::https://github.com/slashid/slashid-ai-forwarders.git//anthropic/deploy/terraform?ref=${{ github.ref_name }}"`
```

- [ ] **Step 2: Add the workspace member to CI** — in `.github/workflows/ci.yml`, extend the matrix and harden the sync:

```yaml
      matrix:
        subproject: [shared, bedrock, vertex, anthropic]
```

```yaml
      - name: Sync workspace
        # Workspace-wide sync so each member can resolve its shared dep.
        # --locked: a plain sync silently rewrites a stale uv.lock, and
        # the anthropic Dockerfile runs ``uv sync --frozen``, so a stale
        # lock would fail the release build instead of this job.
        run: uv sync --all-groups --locked
```

- [ ] **Step 3: Make the terraform job cover both modules** — replace the `terraform` job's single working directory with a matrix:

```yaml
  terraform:
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        dir: [vertex/deploy/terraform, anthropic/deploy/terraform]
    defaults:
      run:
        working-directory: ${{ matrix.dir }}
    steps:
      - uses: actions/checkout@v4

      - name: Install Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "1.15.8"

      - name: Terraform fmt (recursive)
        run: terraform fmt -check -recursive

      - name: Terraform init (no backend)
        run: terraform init -backend=false

      - name: Terraform validate
        run: terraform validate
```

- [ ] **Step 4: Check the lock is current** — `uv lock --check`. Expected: `Resolved N packages … lockfile is up to date` and exit 0. If it fails, `uv lock` and commit the result with the workflow change.

- [ ] **Step 5: Commit**

```bash
git add .github uv.lock
git commit -m "ci(anthropic): release the image to ghcr and run the member in ci"
```

### Task 8.6: READMEs

> **`anthropic/README.md` already exists and is not a blank page.** It carries a
> "What we wish the provider gave us" section — eight measured limitations, each
> with the measurement behind it — written while the plan was being reviewed.
> Build the rest of the README **around** that section and do not rewrite or
> summarize it. Its content overlaps Known limitations deliberately: that section
> is the outward-facing ask, Known limitations is what a customer must live with.
> If a measurement changes, both move together.

`anthropic/README.md` in the shape of `vertex/README.md` — title, what it is, Scope, **Known limitations**, Development, Configuration, Release — plus a Prerequisites section, because this is the only forwarder whose prerequisites include a person with a specific role clicking something that cannot be undone or backdated.

The Known limitations section is the reason this file exists. Vertex's version sets the standard — "not bugs, gotchas to plan around" — and every measured number below is from the design's own corpus. Write them as measured facts, not as caveats.

**Files:**
- Create: `anthropic/README.md`
- Modify: `README.md` (root)

- [ ] **Step 1: Write the head of `anthropic/README.md`** — title, the two-capability paragraph, and Prerequisites:

  - What it is: a customer-deployed Cloud Run service that observes Claude Enterprise AI invocations two ways — an inline **Inference hooks** endpoint that also answers allow/deny, and a scheduled reader of the **Compliance API** — normalizes each into `AIInvocationObservedV1` and pushes to the SlashID NHI subgraph. Deployed via `deploy/terraform/`; `deploy/dev-deploy.sh` is the iteration path against a test tenant.
  - **Capabilities follow the credentials.** `SLASHID_HOOK_SIGNING_SECRET` enables the hook, `SLASHID_COMPLIANCE_KEY` enables the readers, at least one is required, and hook-only, compliance-only and both are configurations of one image.
  - **Prerequisites**, verbatim in substance from the design: Claude Enterprise, and `organization:manage` (Owner or Primary owner) to configure the hook; an `https://` endpoint on port 443, publicly routable, valid public CA certificate, no redirects, no reverse tunnels. For compliance: the Compliance API enabled **by the primary owner**, and a Compliance Access Key with `read:compliance_activities` and `read:compliance_user_data`. For both: a SlashID push token for an **`anthropic`** connection — one deployment, one token. Say why splitting the halves across deployments is not supported: two deployments cannot share a pending store, so the join disappears; content addressing still makes both compute the same `request_id`, so a shared token leaves the terminal's dedup to collapse the overlap and only the enrichment is lost, while **separate connections** make the dedup key `{org}:{conn}:{request_id}` differ and every invocation both halves saw is counted twice.

- [ ] **Step 2: Write the Known limitations section** — this text, which is the design's section carried over:

```markdown
## Known limitations

Measured against a live tenant, not anticipated. Not bugs — gotchas to
plan around. Extend as new ones surface.

- **Two sources can only be joined on a `tool_use` id.** Transcript-prefix
  digests, measured on one session present in both feeds, produced 200 keys
  from the frames and 302 from the reader with **zero in common**: the
  stored transcript is a different projection of the conversation — a
  prepended synthetic marker, turns from before capture was enabled,
  sub-agent turns. Only the model-minted `tool_use.id` survives both. So a
  run with no tool call is **owned by the hook alone**, a run the hook never
  saw is **never recorded**, and the tool-bearing runs — the ones that touch
  files and servers — are covered twice. Under a rollout percentage below
  100 with no compliance key, the unsampled turns are simply absent.
- **Attachment digests reach fewer rounds than attachments.** Enrichment
  needs a joinable run, and only **6 of 14** measured claude.ai rounds had
  one. The other 8 keep the frame's extracted-text digest: exact for plain
  text, absent for a processed document.
- **A digest is of what Claude stored, not always of what was uploaded.**
  A measured image came back 2 KB larger as a processed copy, and some
  documents are stored as extracted text. Such a hash will not match the
  original file, and nothing marks which is which.
- **Denials are sticky.** Under enforcement the denied content stays in the
  transcript and keeps being denied: the verdict scans the round after the
  last assistant message, a denial prevents an assistant message, so the
  offending block stays in scope and **every later turn in that session is
  denied**, however innocuous. The session is unrecoverable; only a new
  conversation escapes, which is why `deny_reason` says so. One incident
  therefore emits one denial event per subsequent turn — group them on
  `conversation_id` plus the `accessed_files` digests.
- **Compliance API enablement is not retroactive.** Nothing that happened
  before the primary owner enabled it is recorded, ever. There is no
  backfill.
- **No tool or MCP-server inventory.** `available_tools` lists only the
  tools actually *used*, by name, with no description and no schema. The
  Bedrock and Vertex forwarders read real declarations from the request
  body, so absence here means unobservable, not unused. On a frame an MCP
  server is visible only when the client names its tools
  `mcp__server__tool`; on claude.ai chat messages the reader does better,
  since tool blocks carry `integration_name` and `mcp_server_url` as
  explicit fields. Server attribution is partly recoverable on one surface,
  tool definitions on neither.
- **No `stop_reason` and no token counts** from any surface. `stop_reason`
  is inferred from the shape of the run; `tokens` is zero.
- **Server-tool results are placeholders**, so content Anthropic's own
  tools fetch is outside every check, and nothing marks a call
  server-executed.
- **claude.ai's extended research emits no frames**, so an agentic task's
  fetches are entirely uninspected.
- **Hash matching from a frame covers plain text only.** The reader closes
  this for files Claude stores intact — subject to the two limits above.
- **`conversation_id` merges sub-conversations.** Haiku status frames and
  `web_search` sub-requests share the main session's id.
- **Reader-emitted events are 10 KB-capped per tool block**, so a
  frame-built record is the better-hashed one wherever both exist.
- **The local-sessions listing cannot be ordered**, so the response reader
  re-walks its whole lagging window every tick instead of resuming from a
  cursor. Dedup absorbs the repeats; a long outage still means a long
  re-walk.
- **Cloud Run caps HTTP/1 bodies at 32 MiB**, below the protocol's 64 MiB
  ceiling. Observed frames peak at 1.86 MB.
- **Ticks overlap.** Cloud Run gives a second concurrent `POST /tick` a
  second instance, and per-instance concurrency does not serialize it. The
  tick takes a Firestore lease and exits if another holds it; that lease,
  not any Cloud Run setting, is what makes overlap safe.
```

- [ ] **Step 3: Write the rest of `anthropic/README.md`**

  - **Development**: `(cd anthropic && uv run pytest)` — the suite runs against a fake Firestore client and recorded API responses, no emulator and no credentials. `./deploy/dev-deploy.sh PROJECT [REGION]` for a live test tenant with frame capture on.
  - **Two failure modes, two owners**: `SLASHID_VERDICT_FAIL_MODE` covers a check that fails or answers unverified; Anthropic's own failure handling covers the case where this service does not answer at all. `SLASHID_SHADOW_MODE` is ours, claude.ai's `shadow_mode` is theirs, and **when either is on nothing is blocked**.
  - **The eventing path never affects a verdict**: the push runs after the response, bounded by `SLASHID_PUSH_BUDGET_MS`, which makes the sink's own `SLASHID_REQUEST_TIMEOUT_SECONDS` and `SLASHID_MAX_RETRIES` largely inert.
  - **Configuration**: one table in `vertex/README.md`'s shape (`| var | required | default |`) covering every field in `config.py` — the inherited `SLASHID_ENDPOINT`, `SLASHID_PUSH_TOKEN`, `SLASHID_INCLUDE_RAW_CONTENT`, `SLASHID_MAX_CONTENT_SIZE`, `SLASHID_REQUEST_TIMEOUT_SECONDS`, `SLASHID_MAX_RETRIES`, plus `LOG_LEVEL` — and marking the ones the Terraform module does not expose as a variable (`SLASHID_CAPTURE_BUCKET`, `SLASHID_CAPTURE_DENY_MARKER`, `SLASHID_REQUEST_TIMEOUT_SECONDS`, `SLASHID_MAX_RETRIES`) as container-env-only. Note the row for `SLASHID_TOMBSTONE_TTL_SECONDS` carries the inequality.
  - **Rollout**: Anthropic provides staged rollout server-side — shadow mode, a rollout percentage, role exclusions, then enforcement with the customer's choice of fail-open or fail-closed — so use it, and ship `SLASHID_SHADOW_MODE=true`. With a compliance credential present, a low rollout percentage stops being a coverage decision and becomes purely an enforcement one: turns the hook never saw are still emitted by the reader, subject to the join limit above.
  - **Release**: `git tag anthropic-v0.1.0 && git push origin anthropic-v0.1.0` publishes `ghcr.io/slashid/slashid-anthropic-forwarder:0.1.0`; the tag must match `anthropic/pyproject.toml`.

- [ ] **Step 4: Root README** — add to Components:

```markdown
- [`anthropic/`](anthropic/README.md) — Claude Enterprise forwarder on Cloud Run. Inference hooks (inline, with allow/deny) + Compliance API polling → SlashID.
```

and to Releases:

```markdown
- `anthropic-vX.Y.Z` → publishes the receiver container image to GHCR; the Terraform module is consumed from the tag (see `anthropic/README.md`).
```

Add the three `(cd anthropic && …)` lines to the Development block beside bedrock, vertex and shared.

- [ ] **Step 5: Commit**

```bash
git add README.md anthropic/README.md
git commit -m "docs(anthropic): readme and known limitations"
```

### Task 8.7: full gate, container smoke, PR

**Files:** none — this task only verifies and opens the PR.

- [ ] **Step 1: The whole toolchain** — from the repository root:

```bash
uv lock --check && \
for d in shared bedrock vertex anthropic; do \
  (cd "$d" && uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q) || { echo "FAILED: $d"; break; }; \
done
```

Expected: four green blocks. `shared` and `vertex` are not optional here — Chunk 2 changed the shared schema and Chunk 5 promoted `CheckpointStore` out of `vertex/`, so a regression in either shows up nowhere else. `bedrock` rides on the same shared change and is what CI runs.

- [ ] **Step 2: Both Terraform modules** — what the CI matrix runs:

```bash
for d in vertex/deploy/terraform anthropic/deploy/terraform; do \
  (cd "$d" && terraform fmt -check -recursive && terraform init -backend=false >/dev/null && terraform validate) || { echo "FAILED: $d"; break; }; \
done
```

Expected: `Success! The configuration is valid.` twice.

- [ ] **Step 3: Build the image** — `docker build -f anthropic/Dockerfile -t slashid-anthropic-forwarder:dev .`. Expected: a successful build; the `uv sync --frozen` layers are what would fail on a stale `uv.lock`.

- [ ] **Step 4: Smoke the capability rules against the real image** — three runs, no GCP credentials needed since none of them reaches Firestore:

```bash
# (a) no credential at all: startup must refuse.
docker run --rm -e SLASHID_ENDPOINT=https://api.slashid.com -e SLASHID_PUSH_TOKEN=x \
  -e SLASHID_GCP_PROJECT_ID=p slashid-anthropic-forwarder:dev 2>&1 | tail -3

# (b) compliance only: no signing secret, and it starts.
docker run --rm -d --name sid-compliance -p 18081:8080 \
  -e SLASHID_ENDPOINT=https://api.slashid.com -e SLASHID_PUSH_TOKEN=x \
  -e SLASHID_GCP_PROJECT_ID=p -e SLASHID_COMPLIANCE_KEY=sk-ant-api01-x \
  -e SLASHID_ORGANIZATION_UUID=org-1 slashid-anthropic-forwarder:dev
for i in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:18081/ && break; sleep 0.5; done
docker logs sid-compliance | tail -3; docker rm -f sid-compliance

# (c) an hourly tick under the default tombstone TTL: startup must refuse.
docker run --rm -e SLASHID_ENDPOINT=https://api.slashid.com -e SLASHID_PUSH_TOKEN=x \
  -e SLASHID_GCP_PROJECT_ID=p -e SLASHID_HOOK_SIGNING_SECRET=whsec_AAA \
  -e SLASHID_TICK_INTERVAL_SECONDS=3600 slashid-anthropic-forwarder:dev 2>&1 | tail -3
```

Expected: (a) exits non-zero with `no capability configured`; (b) stays up, logs no validation error — this is the deployment the old `_check_signing` made impossible; (c) exits non-zero naming `SLASHID_TOMBSTONE_TTL_SECONDS`.

- [ ] **Step 5: Signed-frame smoke** — one real frame through the hook path, against the fixture corpus:

```bash
docker run --rm -d --name sid-hook -p 18080:8080 \
  -e SLASHID_ENDPOINT=https://api.slashid.com -e SLASHID_PUSH_TOKEN=x \
  -e SLASHID_GCP_PROJECT_ID=p -e SLASHID_PREFLIGHT_ENABLED=false \
  -e "SLASHID_HOOK_SIGNING_SECRET=$(cd anthropic && uv run python -c 'from tests.conftest import SECRET; print(SECRET)')" \
  slashid-anthropic-forwarder:dev
for i in $(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:18080/ && break; sleep 0.5; done
(cd anthropic && uv run python - <<'EOF'
import base64, hashlib, hmac, pathlib, time, urllib.request
from tests.conftest import SECRET

body = pathlib.Path("tests/fixtures/frame_tool_result.json").read_bytes()
msg_id, ts = "req_smoke", str(int(time.time()))
key = base64.b64decode(SECRET.removeprefix("whsec_"))
sig = base64.b64encode(
    hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
).decode()
req = urllib.request.Request(
    "http://127.0.0.1:18080/hooks/anthropic",
    data=body,
    method="POST",
    headers={
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": f"v1,{sig}",
        "content-type": "application/json",
    },
)
print(urllib.request.urlopen(req).read())
EOF
)
docker logs sid-hook | tail -5; docker rm -f sid-hook
```

Expected: `b'{"action":"allow"}'`, and a Firestore error in the logs where the record write failed — with no credentials and project `p` there is nothing to write to, and the verdict going out regardless **is rule 1 working**, not a failure of the smoke.

- [ ] **Step 6: Open the PR**

```bash
git push -u origin paulo/anthropic-receiver
gh pr create --base main --title "feat(anthropic): Claude Enterprise inference hooks and compliance readers" --body "$(cat <<'EOF'
Implements `docs/superpowers/specs/2026-09-21-anthropic-ai-invocations-design-v2.md`.

One Cloud Run service, two capabilities selected by which credentials are
present: the inline Inference hooks endpoint (which also answers
allow/deny) and a scheduled Compliance API reader. Neither pushes from
the request path — both write to a Firestore pending store addressed by
content, and a claim decides who pushes.

- `shared/`: Anthropic schema additions, `AIAccessedFile.provenance`,
  `EventEnvelope.conversation_id`, `CheckpointStore` promoted out of
  `vertex/`.
- `anthropic/`: the frame parser and the one-round-behind attribution,
  the verdict path, the address/record/store spine, the two readers, the
  Terraform module and the release workflow.

Known limitations are in `anthropic/README.md`; the ones that constrain
coverage rather than polish are the `tool_use`-only join between the two
sources and the 6-of-14 attachment enrichment rate, both measured.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_018nh5xWtVeSm8Hmdkxf3Z1A
EOF
)"
```

- [ ] **Step 7: STOP.** Do not merge, do not enable auto-merge, do not run `gh pr merge` in any form. Print the PR URL and hand back. The merge is the user's call, per PR, and they give it explicitly — a green CI run is not that approval.

---
