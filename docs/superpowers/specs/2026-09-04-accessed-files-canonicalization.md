# Phase 2.1: Canonicalize `accessed_files`, split boto out of `shared/`

**Date:** 2026-09-04
**Status:** Design draft, ready for review
**Predecessors (shipped):**

- Phase 2 (canonical `NormalizedInvocation` — `bedrock-v0.1.2`) — PR #14.
- Phase 2 smoke-test additions (`./converse-attach` + Read-tool recipe) — PR #15.

**Supersedes:**

- PR #16 (fix: skip `accessed_files` entry when `tool_result` reports error) — the `is_error` guard is subsumed into the new tool-result extractor and lands with this refactor.

## Context

Phase 2 moved most of `build_event`'s per-record traversal onto the canonical `NormalizedInvocation` (tokens, stop_reason, tools_declared/tool_servers, used_tools). One fossil remained: `_accessed_files(record, ...)` in `shared/src/slashid_ai_forwarder_core/events.py` still walks the raw MIL record and does three separable things in one place:

1. **Bedrock Converse attachment extraction** — reads `{document: {...}}` / `{image: {...}}` blocks from `record["input"]["inputBodyJson"]["messages"]` and either decodes inline base64 bytes or resolves `{s3Location: {uri}}` sources via `shared/s3.py::_resolve_s3_attachment` (async, HeadObject + optional GetObject).
2. **Read-tool tool-result hashing** — walks the raw record for `_READ_TOOLS`-matching tool_use/tool_result pairs, applies `strip_cat_n` cleanup, hashes the returned bytes under the tool's `file_path` argument.
3. **Wire-model construction** — assembles the resulting `AIAccessedFile` entries onto `AIInvocationObservedV1.accessed_files`.

Three problems:

- **Bedrock-specific extraction lives in shared code.** Converse's document/image blocks aren't a canonical concept; they're a wire quirk of one vendor. Vertex Gemini has its own attachment shape, Anthropic native has none. Handling all of them from `shared/events.py` doesn't scale.
- **Read-tool extraction re-parses raw record shapes** (both Anthropic and Converse) even though the canonical `NormalizedInvocation.input.messages` already normalizes both into `NormalizedContent(kind="tool_use"/"tool_result")` blocks. Duplicated shape-awareness.
- **`shared/` transitively depends on `aioboto3`** solely for Bedrock's S3 attachment resolver. `shared/pyproject.toml`'s comment on the `aioboto3` line already flags this as a Phase 2 cleanup.

Additionally, live smoke-testing (PR #15) confirmed a data-quality bug: when Claude Code's `Read` tool errors (e.g. permission-refused), the tool_result content is an error-message body, and `_accessed_files` hashes it under the requested `file_path` — misleading any downstream consumer that reads `accessed_files.content_hashes` as "the file's real content." PR #16 proposed a targeted `is_error` guard; this refactor absorbs that guard into the new tool-result extractor.

## Target architecture

Three well-separated units, each with a single responsibility:

```
                    NormalizedInvocation (canonical, shared)
                    ├── input: messages, tools_declared, tool_servers
                    ├── output: message, stop_reason
                    ├── tokens
                    └── accessed_files: list[AIAccessedFile] | None    ← NEW
                          ▲                          ▲
                          │                          │
    ┌─────────────────────┴──────┐   ┌──────────────┴────────────────────┐
    │ bedrock/converse_attachments│  │ shared/normalize/normalized/       │
    │ (async, boto)               │  │   tool_results.py (sync, no boto)  │
    │                             │  │                                    │
    │ walks record's Converse     │  │ walks normalized.input.messages    │
    │ {document}/{image} blocks;  │  │ for _READ_TOOLS-matching pairs;    │
    │ decodes inline bytes;       │  │ hashes tool_output; is_error guard │
    │ HeadObject/GetObject on     │  │                                    │
    │ {s3Location: {uri}} sources │  │                                    │
    └─────────────────────────────┘  └────────────────────────────────────┘
                          │                          │
                          └──────────┬───────────────┘
                                     ▼
                     shared/events.py::build_event
                     just reads normalized.accessed_files
                     (no I/O, no boto, no async needed here)
```

**Boundary rule:** anything vendor-specific and I/O-bearing (Converse block shapes, S3 fetches, MIL offload resolution) belongs in `bedrock/`. Anything vendor-agnostic and pure (walking the canonical, hashing bytes, dedup) belongs in `shared/`. `build_event` is the pure envelope-assembler; it never reaches out.

## Canonical type change

Add one field to `NormalizedInvocation` in `shared/src/slashid_ai_forwarder_core/normalize/normalized/types.py`, and — drive-by — flip every list-typed field on both `NormalizedInvocation` and `NormalizedInvocationInput` from nullable-default-None to non-nullable-default-`[]`:

```python
class NormalizedInvocationInput(_LenientModel):
    messages: list[NormalizedMessage] = Field(default_factory=list)          # was: | None = None
    tools_declared: list[AITool] = Field(default_factory=list)               # was: | None = None
    tool_servers: list[AIToolServer] = Field(default_factory=list)           # was: | None = None


class NormalizedInvocation(_LenientModel):
    tokens: AIInvocationTokens = Field(default_factory=AIInvocationTokens)
    input: NormalizedInvocationInput = Field(default_factory=NormalizedInvocationInput)
    output: NormalizedInvocationOutput = Field(default_factory=NormalizedInvocationOutput)
    accessed_files: list[AIAccessedFile] = Field(default_factory=list)       # NEW
```

**Placement rationale.** `accessed_files` sits top-level (not nested under `input`) — it accumulates from two very different sources (attachments in messages, tool_result contents) and the traversal never needs the input/output split. Top-level also mirrors the wire shape (`AIInvocationObservedV1.accessed_files` is top-level).

**Non-null default rationale.** These fields are always written by the vendor's `to_normalized_invocation` (messages/tools_declared/tool_servers) or by `finalize` (accessed_files) — there's no meaningful "unpopulated" state, and consumers currently paper over the None case with `or []` guards at every read. Non-null default drops those guards. The wire-model boundary keeps its `or None` at construction (`accessed_files=normalized.accessed_files or None`, `available_tools=normalized.input.tools_declared or None`, etc.) so `AIInvocationObservedV1.model_dump(mode="json", exclude_none=True)` still omits empty lists on the wire — no wire-behavior change.

**Callsite simplifications this enables** (all get done as part of the same chunk):

- `converse/normalize.py::_to_input` and `anthropic/normalize.py::_request_to_input`: drop the `or None` on their `NormalizedInvocationInput(messages=... or None, ...)` construction sites. Just pass the built lists.
- `shared/events.py::_used_tools` and `build_event`: drop the `or []` on `normalized.input.messages` / `tools_declared` / `tool_servers` reads. Traverse the lists directly.
- Wire-event boundary in `build_event`: still uses `or None` on the field-assignment side, preserving current wire semantics.

**No wire model changes.** `AIAccessedFile` in `shared/src/slashid_ai_forwarder_core/events.py` stays exactly as-is. `AIInvocationObservedV1.accessed_files` stays exactly as-is. `build_event` just reads `normalized.accessed_files` and assigns it to the wire event's `accessed_files` field (with `or None`).

**No new `NormalizedContent` kinds.** The `Literal["text", "image", "audio", "document", "tool_use", "tool_result", "reasoning"]` union already includes `image` and `document` from Phase 2, but they were forward-looking placeholders — no code populates them today, and no consumer reads them. This refactor leaves them unused. Attachments are metadata *about* the invocation (what files were touched), not part of the conversational flow the LLM sees as messages. Modeling them as `NormalizedContent` blocks would require adding a `raw_bytes: bytes | None` field to `NormalizedContent` and inflating every attachment into a message-nested block, which is a bigger canonical extension and doesn't buy anything build_event needs. If a future consumer needs attachments-as-content-blocks (e.g. for prompt-injection scanning), we revisit — but not now.

## Module layout

### New modules

**`shared/src/slashid_ai_forwarder_core/normalize/normalized/otel.py`** — leaf module for `_extract_otel` + its helpers (`_otel_from_dict`, `_otel_from_text`, `_OTEL_KEY`, `_TRACE_ID_HEX`, `_SPAN_ID_HEX`, `_OTEL_MARKER`). Depends on nothing internal. Exists as a separate module — not inside `tool_results.py` — because both `shared/events.py::_used_tools` AND `tool_results.py::extract_tool_result_files` need it. Putting the helpers in `tool_results.py` would force `events.py` to import from `tool_results.py`, and `tool_results.py` already imports `AIAccessedFile` from `events.py` → cycle. Leaf module breaks it: `events.py` and `tool_results.py` both import from `otel.py`; `otel.py` imports from neither.

**`shared/src/slashid_ai_forwarder_core/normalize/normalized/tool_results.py`** — vendor-agnostic post-hook. Single public function:

```python
def extract_tool_result_files(
    messages: list[NormalizedMessage],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    """Walk messages for _READ_TOOLS-matching tool_use/tool_result pairs.

    Correlates via tool_use_id, applies per-tool cleanup (e.g. strip_cat_n
    for Claude Code Read), hashes tool_output bytes, returns AIAccessedFile
    entries. Skips pairs where tool_is_error is True — on error paths
    tool_output is the error message body, not file bytes, and hashing it
    would attribute the error string to the file path.

    Only tool_results after the last assistant message are considered
    (same "fresh region" rule as _used_tools — earlier results were
    already reported on prior invocations).
    """
```

**`bedrock/src/slashid_bedrock_forwarder/converse_attachments.py`** — bedrock-side, async. Owns the raw-record → `AIAccessedFile` extraction for Converse document/image blocks. Two public entry points:

```python
async def extract_converse_attachments(
    record: dict[str, Any],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    """Walk record["input"]["inputBodyJson"]["messages"] for {document}/
    {image} blocks. Decodes inline base64 bytes; resolves
    {s3Location: {uri}} sources via HeadObject + optional GetObject
    (gated by MAX_FETCH_BYTES). Returns AIAccessedFile entries."""
```

**`bedrock/src/slashid_bedrock_forwarder/s3.py`** — verbatim move of `shared/s3.py`. Owns `resolve_offloaded_bodies` (MIL body offload), `fetch_offloaded_body`, `_resolve_s3_attachment` (formerly called from `shared/events._accessed_files`), `MAX_PARALLEL_FETCHES`, `MAX_FETCH_BYTES`. The attachment resolver becomes an internal detail of `converse_attachments.py`.

### Wrapper module — enforces the "always finalize" contract

**`shared/src/slashid_ai_forwarder_core/normalize/finalize.py`** — one function, deliberately small:

```python
def finalize(
    normalized: NormalizedInvocation,
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> NormalizedInvocation:
    """Vendor-agnostic post-hook — single-pass.

    Appends tool-result files (from ``extract_tool_result_files``) to
    ``normalized.accessed_files``. Callers MUST invoke this exactly once
    per invocation, after any vendor-side attachment extraction (e.g.
    ``bedrock.converse_attachments.extract_converse_attachments``) has
    populated ``normalized.accessed_files``. Not idempotent: a second
    call would re-append the same tool-result files (``extract_tool_
    result_files`` dedups within a single call but doesn't compare
    against ``normalized.accessed_files``).
    """
    tool_files = extract_tool_result_files(
        normalized.input.messages,
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )
    normalized.accessed_files.extend(tool_files)
    return normalized
```

**Why a wrapper, not per-vendor hooks:** each vendor's `to_normalized_invocation` stays pure (request+response → canonical shape, no cross-cutting extraction). The finalize step is a discrete post-processing pass that only depends on the canonical shape, so it's called once per invocation from the shared boundary. This matches the design decision from the earlier tools_declared refactor: vendor normalizers do vendor stuff; cross-cutting analysis reads canonical.

Bedrock's handler composes:

```python
normalized = normalize_record(record)
normalized.accessed_files = await extract_converse_attachments(
    record, include_raw_content=..., max_content_size=...
) or None
normalized = finalize(normalized, include_raw_content=..., max_content_size=...)
event = await build_event(normalized, record, ...)   # no more async pressure inside build_event
```

Future non-Bedrock forwarders (Vertex, audit-envelope) skip the attachment step (they have no such concept) and just call `finalize`.

## Data flow (post-refactor handler)

```python
# bedrock/src/slashid_bedrock_forwarder/handler.py::_run
async def _run(records: list[dict[str, Any]], config: Config) -> dict[str, int]:
    from .s3 import resolve_offloaded_bodies                           # moved from shared
    from .converse_attachments import extract_converse_attachments     # new

    await resolve_offloaded_bodies(records)

    async def _prepare(record):
        normalized = normalize_record(record)                          # unchanged
        attachments = await extract_converse_attachments(
            record,
            include_raw_content=config.include_raw_content,
            max_content_size=config.max_content_size,
        )
        normalized.accessed_files.extend(attachments)
        normalized = finalize(
            normalized,
            include_raw_content=config.include_raw_content,
            max_content_size=config.max_content_size,
        )
        return normalized, record

    prepared = await asyncio.gather(*(_prepare(r) for r in records))
    built_or_none = await asyncio.gather(
        *(
            build_event(normalized, record,
                        include_raw_content=config.include_raw_content,
                        max_content_size=config.max_content_size)
            for normalized, record in prepared
        )
    )
    events = [e for e in built_or_none if e is not None]
    ...
```

**Concurrency:** `extract_converse_attachments` internally uses `asyncio.Semaphore(MAX_PARALLEL_FETCHES)` to cap S3 fan-out per record (existing behavior, preserved). The outer `asyncio.gather` over `_prepare` calls runs record-level preparation concurrently, same as today.

**`build_event`:**

- Signature stays `async def build_event(normalized, record, *, ...) -> AIInvocationObservedV1 | None`. The `async` becomes redundant on the file-hashing path (no more `await`) and, post-refactor, `build_event` performs no I/O at all. Keeping the `async` signature avoids rippling into every test callsite; drop-async is a separate cleanup PR (see Non-goals).
- Reads `normalized.accessed_files` and assigns to the wire event's `accessed_files` field.
- Deletes: `_accessed_files`, `_READ_TOOLS`, `_ToolSpec` (relocated to `tool_results.py`); `_extract_otel`, `_otel_from_dict`, `_otel_from_text`, `_OTEL_KEY`/`_TRACE_ID_HEX`/`_SPAN_ID_HEX`/`_OTEL_MARKER` (relocated to the leaf `otel.py` — needed by both `_used_tools` and `extract_tool_result_files`). About 400 lines migrate out of `events.py`.

## Tool-result extractor detail

`extract_tool_result_files` in `shared/normalize/normalized/tool_results.py`:

```python
def extract_tool_result_files(
    messages: list[NormalizedMessage],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    if not messages:
        return []

    # 1. tool_use_id → (raw tool_name, input) — from any assistant tool_use block
    tool_use_by_id: dict[str, tuple[str, JsonValue]] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if block.kind == "tool_use" and block.tool_use_id and block.tool_name:
                tool_use_by_id[block.tool_use_id] = (block.tool_name, block.tool_input)

    # 2. Fresh region: after the last assistant message
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.role == "assistant"),
        default=-1,
    )

    # 3. For each tool_result in fresh region, correlate + hash.
    # Dedup key is (name, sha256) — matches Phase 1 behaviour. Edge case:
    # if content bytes couldn't be derived (empty tool_output, list of
    # non-text blocks, etc.), sha256 falls to None, and multiple
    # different tool_results for the same path collapse to one entry.
    # Rare; matches existing behaviour; live-safe.
    out: list[AIAccessedFile] = []
    seen: set[tuple[str, str | None]] = set()
    for msg in messages[last_assistant + 1 :]:
        for block in msg.content:
            if block.kind != "tool_result" or not block.tool_use_id:
                continue
            if block.tool_is_error:
                # NEW GUARD — see design context
                continue

            pair = tool_use_by_id.get(block.tool_use_id)
            if not pair:
                continue
            tool_name, tool_input = pair
            spec = _READ_TOOLS.get(tool_name)
            if not spec:
                continue

            path = _get_field(tool_input, spec.field_name)
            if not path:
                continue

            content_bytes = _bytes_from_tool_output(block.tool_output, spec.cleanup)
            file = _build_accessed_file(
                name=path,
                content_bytes=content_bytes,
                include_raw_content=include_raw_content,
                max_content_size=max_content_size,
            )
            key = (path, file.content_hashes.get("sha256") if file.content_hashes else None)
            if key in seen:
                continue
            seen.add(key)
            out.append(file)
    return out
```

**OTel context extraction goes in a leaf module.** `_extract_otel` and its helpers live in `normalize/normalized/otel.py` (see "Module layout"). Both `shared/events.py::_used_tools` AND `tool_results.py::extract_tool_result_files` import from `otel.py`. `_used_tools` drops its inline definition — no logic change, just a relocation.

**`_READ_TOOLS` moves to `tool_results.py`.** Only `extract_tool_result_files` reads it. No cycle risk here — `tool_results.py` is the sole consumer.

**`AIAccessedFile` still comes from `events.py`.** It's a wire model; stays where the wire models live. `tool_results.py` imports it (one-way — `events.py` does NOT import from `tool_results.py`; that would close the cycle B1 was originally about).

## Bedrock-side attachment extractor detail

`extract_converse_attachments` in `bedrock/converse_attachments.py`:

```python
async def extract_converse_attachments(
    record: dict[str, Any],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    body = (record.get("input") or {}).get("inputBodyJson")
    if not isinstance(body, dict):
        return []
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    if not messages:
        return []

    # Fresh region: files in earlier turns were already reported.
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1,
    )
    fresh_messages = messages[last_assistant + 1 :]

    # Collect S3 sources and resolve them concurrently before building the list.
    s3_sources = _collect_s3_sources(fresh_messages)
    if s3_sources:
        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)
        async def _guarded(src):
            async with sem:
                await _resolve_s3_attachment(src, max_content_size=max_content_size)
        await asyncio.gather(*(_guarded(s) for s in s3_sources))

    # Build AIAccessedFile entries — inline bytes decoded or S3-resolved.
    return _build_files_from_messages(
        fresh_messages,
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )
```

Structurally, this is `shared/events._accessed_files` minus the tool-result section, moved to bedrock, and reworked to `return` a list instead of accumulating on a captured local. All the format-mapping (`_DOC_MIME`, `_mime_from_name`), inline base64 decoding, and S3 HEAD/GET plumbing (`_resolve_s3_attachment` and friends) live in `bedrock/` alongside. Behavior identical.

## Migration order

Five chunks, each on its own commit:

**Chunk 1 — Canonical type field + list-default drive-by.** Add `NormalizedInvocation.accessed_files: list[AIAccessedFile] = Field(default_factory=list)`. Flip `NormalizedInvocationInput.{messages, tools_declared, tool_servers}` from `list[...] | None = None` to `list[...] = Field(default_factory=list)`. Update the two vendor `_to_input` / `_request_to_input` sites to drop `or None`. Update `shared/events.py::_used_tools` and `build_event` to drop `or []` on canonical reads (keep `or None` on wire-event assignment). No populators for `accessed_files` yet; no consumers yet. Extend `test_types_round_trip.py` for the new field and for the non-null-default behaviour.

**Chunk 2 — Shared OTel leaf + tool-result extractor.** Create `shared/normalize/normalized/otel.py` with `_extract_otel` + helpers (`_otel_from_dict`, `_otel_from_text`, `_OTEL_KEY`, regex constants). Create `shared/normalize/normalized/tool_results.py` with `extract_tool_result_files`, `_READ_TOOLS`, `_ToolSpec`. Both import `_extract_otel` from the leaf `otel.py`. Update `shared/events.py::_used_tools` to import `_extract_otel` from `normalize/normalized/otel.py` (no logic change) and delete the inline copy. Add tests for both new modules.

**Chunk 3 — Shared finalize wrapper.** Create `shared/normalize/finalize.py` with `finalize()`. Tests: single-pass semantics, empty-invocation no-op, tool-result files land on `normalized.accessed_files`, is_error skip.

**Chunk 4 — Atomic switch (bedrock-side extractor + drop shared boto + rewire handler).** Everything the layering split requires, in one commit — an intermediate chunk that moved `shared/s3.py` without also flipping the callers would introduce a shared→bedrock import (layering violation) that a follow-up commit would immediately delete, so merge the moves.

Specifically:

- Move `shared/s3.py` → `bedrock/src/slashid_bedrock_forwarder/s3.py` verbatim; move `shared/tests/test_s3.py` → `bedrock/tests/test_s3.py`.
- Create `bedrock/src/slashid_bedrock_forwarder/converse_attachments.py` with `extract_converse_attachments` and the document/image walker that used to live inline in `shared/events._accessed_files` (minus the tool-result section, which lives in `shared/normalize/normalized/tool_results.py` from Chunk 2).
- Delete from `shared/events.py`: `_accessed_files`, `_READ_TOOLS`, `_ToolSpec`, `_extract_otel` + all OTel helper regexes/consts (all relocated in Chunk 2; Chunk 4 completes the deletion once nothing in shared refers to them anymore).
- Simplify `shared/events.py::build_event`: `accessed_files=normalized.accessed_files or None` on the wire event construction.
- Update `shared/events.py::_used_tools`: import `_extract_otel` from `normalize/normalized/otel.py` (from Chunk 2); no logic change.
- Update `bedrock/handler.py::_run` to compose the new pipeline (see "Data flow" section).
- `shared/pyproject.toml`: remove `"aioboto3>=15.0"` (and its comment).
- `bedrock/pyproject.toml`: add `"aioboto3>=15.0"`.
- Migrate `shared/tests/test_events.py::test_accessed_files_*` (21 tests total). Split: 14 attachment-focused → `bedrock/tests/test_converse_attachments.py`; 6 tool-result-focused → subsumed by `test_tool_results.py` fixtures from Chunk 2; 1 cross-path (`test_accessed_files_dedup_tool_and_attachment`) → `bedrock/tests/test_handler.py` end-to-end (only chunk where both extractors run together). Exact per-case mapping goes in the implementation plan.
- Any remaining `shared/tests/test_events.py` tests that referenced `_accessed_files` internals get updated to just assert on `event.accessed_files` produced by the composed pipeline via the fixture builders.

**Chunk 5 — Release.** Bump `bedrock/pyproject.toml` version `0.1.2` → `0.1.3`. Update `bedrock/README.md` smoke-test section — the `./converse-attach` and Read-tool recipes still work; no user-facing change. PR body notes the wire compatibility.

**Sequencing notes.**

- Chunks 1–3 add new code without disturbing existing behavior. Old `_accessed_files(record)` path still runs and populates `AIInvocationObservedV1.accessed_files` as before. New canonical field is `None`.
- Chunk 4 flips the switch atomically: bedrock-side extractor + finalize wrapper populate `normalized.accessed_files`; `build_event` reads from there. Old `_accessed_files` is deleted in the same commit.
- Chunk 5 ships.

## Testing

**Shared side (all sync, no boto):**

- `test_tool_results.py` — end-to-end YAML fixture cases for `extract_tool_result_files`: Read cat-n content, ReadFile plain content, is_error skip (both Anthropic and Converse shapes exercised via canonical), tool_use with no matching result (deferred), tool_result with no matching tool_use (skipped), unknown tool name (skipped), OTel context extraction from tool_output.
- `test_finalize.py` — Python tests for single-pass semantics (a second call double-appends — documented non-idempotent behaviour), empty-invocation no-op, dedup within a single call (a file surfaced by both bedrock-side attachment path and tool-result path should appear once — same `(name, sha256)` key).
- `test_types_round_trip.py` — extended to cover `NormalizedInvocation.accessed_files` round-trip.
- Existing `test_events.py::test_accessed_files_*` cases (21 tests). Split: (a) 6 tool-result-focused (`tool_result_read`, `tool_result_read_no_prefix_falls_back`, `tool_result_raw_content_opt_in`, `tool_result_readfile_variant`, `unknown_tool_ignored`, `tool_result_only_last_turn`) subsumed by `test_tool_results.py` fixtures from Chunk 2, plus the 2 new is_error tests (Anthropic + Converse) from the retired PR #16; (b) 14 attachment-focused (`document_inline`, `document_raw_content_opt_in`, `image_inline`, `s3_source_uses_uri_as_name`, `s3uri_shape`, `s3_content_type_used_as_media_type_fallback`, `mime_map`, `media_type_from_filename_fallback`, `stub_has_media_type_from_filename`, `non_dict_input_body_returns_empty`, `deduplicates_within_same_window`, `only_from_last_user_turn`, `all_included_when_no_prior_assistant_turn`, `none_when_no_attachments`) migrate to `bedrock/tests/test_converse_attachments.py`; (c) 1 cross-path (`dedup_tool_and_attachment`) migrates to `bedrock/tests/test_handler.py` since it's the only place both extractors run together. 6+14+1 = 21.

**Bedrock side (async, boto-backed):**

- `bedrock/tests/test_converse_attachments.py` — receives the migrated document/image attachment tests. Same fixtures as before (base64-inlined bytes, S3-source mocks via `moto` or the existing `monkeypatch.setattr(s3, "fetch_offloaded_body", ...)` pattern from `shared/tests/test_s3.py`). Async pytest.
- `bedrock/tests/test_s3.py` — verbatim move of `shared/tests/test_s3.py` (13 tests).
- `bedrock/tests/test_handler.py` — extended if needed to assert `accessed_files` populates via the new pipeline. Given the handler is thin composition and each unit is tested individually, one end-to-end sanity test is enough (`test_run_end_to_end_populates_accessed_files`).

**Regression coverage:**

- Round-trip diff against a captured live MIL record (both Nova + Claude, both with and without attachments) — before merge. `AIInvocationObservedV1.accessed_files` must be identical entry-for-entry, hash-for-hash, to the pre-refactor emission. The one intentional difference: entries where `used_tools[i].is_error == True` for a `_READ_TOOLS`-matching pair drop out (PR #16's fix, now baked in).
- Live smoke retest against the deployed Lambda after Chunk 5 ships: re-run `./converse-attach ./notes.txt`, `./converse-attach ./chart.png`, and `./claude "please Read SMOKE_TARGET.txt ..."` — event JSON must show the same three `accessed_files` entries as the pre-refactor runs.

## Verification (per chunk)

1. **Chunk 1:** `cd shared && uv run pytest -q` — types round-trip + all previous shared tests green.
2. **Chunk 2:** `cd shared && uv run pytest -q` — new `test_tool_results.py` green + existing suite unchanged.
3. **Chunk 3:** `cd shared && uv run pytest -q` — new `test_finalize.py` green.
4. **Chunk 4:** `(cd shared && uv run pytest -q) && (cd bedrock && uv run pytest -q)` — both green. `grep -rn "aioboto3\|from .s3 import" shared/` returns nothing. `grep -rn "_accessed_files\|_READ_TOOLS" shared/src/` returns nothing (moved out). `uv run ty check` clean. `uv run ruff check shared/ bedrock/` clean. `(cd shared && uv run ruff format --check .) && (cd bedrock && uv run ruff format --check .)` clean.
5. **Chunk 5:** `bedrock-v0.1.3` deployable; live smoke reruns pass.

## Rollback

Standard: `cloudformation update-stack` back to `bedrock-v0.1.2`. No wire schema change → server-side unaffected. Downstream consumers see identical event shape (same fields, same value semantics, only difference is the is_error skip that was already flagged as correct behavior).

## Non-goals

- **New `NormalizedContent` kinds populated with attachment bytes.** The `image`/`document`/`audio` kinds stay unused for now. If a future consumer needs attachments-as-content-blocks (e.g. prompt-injection scanning), we revisit — but not this refactor.
- **Dropping `async` from `build_event`.** Deferred to a follow-up. `build_event` retains its `async` signature to avoid rippling into ~40 test callsites; drop-async is mechanical and independently reviewable.
- **Vertex / audit-envelope attachment shapes.** Vertex Gemini has `parts[].inlineData` (base64 + mimeType); OpenAI Responses has `input_image` / `input_file`; Purview has `AccessedResources`. Each future forwarder will add its own `extract_<vendor>_attachments` module in its own subproject following the same pattern. Out of scope here.
- **Wire schema extensions.** `AIAccessedFile` stays as-is. Future fields (executor, sensitivity_labels, dlp_verdict) batch with the ng-evangelion spec sync per memory `wire_schema_extensions_batched.md`.

## Critical files touched

**New:**

- `shared/src/slashid_ai_forwarder_core/normalize/normalized/otel.py` — leaf module for `_extract_otel` + regex/const helpers; both `events.py::_used_tools` and `tool_results.py` import from here (breaks the would-be cycle described in "Module layout").
- `shared/src/slashid_ai_forwarder_core/normalize/normalized/tool_results.py` — vendor-agnostic tool-result extractor + `_READ_TOOLS`.
- `shared/src/slashid_ai_forwarder_core/normalize/finalize.py` — `finalize(normalized, ...)` post-hook wrapper.
- `shared/tests/normalize/normalized/test_otel.py` — leaf-module unit tests.
- `shared/tests/normalize/normalized/test_tool_results.py` + `.yaml` fixture.
- `shared/tests/normalize/test_finalize.py` — note: one directory level up, mirroring the source layout (`shared/src/.../normalize/finalize.py` lives at the `normalize/` root, not under `normalized/`).
- `bedrock/src/slashid_bedrock_forwarder/s3.py` — moved from `shared/`.
- `bedrock/src/slashid_bedrock_forwarder/converse_attachments.py` — `extract_converse_attachments` + inline-bytes + S3-source handling for Converse document/image blocks.
- `bedrock/tests/test_converse_attachments.py` — migrated from `shared/tests/test_events.py::test_accessed_files_*` (attachment-focused subset).
- `bedrock/tests/test_s3.py` — verbatim move.

**Modified:**

- `shared/src/slashid_ai_forwarder_core/normalize/normalized/types.py` — add `NormalizedInvocation.accessed_files`.
- `shared/src/slashid_ai_forwarder_core/events.py` — delete `_accessed_files`, `_READ_TOOLS`, `_ToolSpec`, `_extract_otel` + helpers; simplify `build_event` to read `normalized.accessed_files`; update `_used_tools` to import `_extract_otel` from `normalize/normalized/otel.py` (NOT from `tool_results.py` — that would close the events↔tool_results cycle described in "Module layout").
- `shared/pyproject.toml` — remove `aioboto3` dep.
- `shared/tests/test_events.py` — remove attachment-focused tests (migrated to bedrock); update remaining tests that referenced the deleted internals.
- `shared/tests/normalize/normalized/test_types_round_trip.py` — cover the new field.
- `bedrock/src/slashid_bedrock_forwarder/handler.py::_run` — compose `normalize_record` + `extract_converse_attachments` + `finalize` before `build_event`.
- `bedrock/tests/test_handler.py` — one end-to-end sanity test for the composed pipeline.
- `bedrock/pyproject.toml` — add `aioboto3` dep; version bump `0.1.2` → `0.1.3`.
- `bedrock/README.md` — smoke-test section: `./converse-attach` and Read-tool recipes unchanged; note the is_error data-quality improvement.

**Deleted:**

- `shared/src/slashid_ai_forwarder_core/s3.py` — moved.
- `shared/tests/test_s3.py` — moved.
