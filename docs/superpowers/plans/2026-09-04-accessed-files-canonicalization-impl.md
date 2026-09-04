# Phase 2.1 — Canonicalize `accessed_files` + boto split — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move `accessed_files` extraction off the raw MIL record and onto `NormalizedInvocation`; split Bedrock-specific attachment handling into `bedrock/`; drop `aioboto3` from `shared/`. Includes a drive-by: all `list[...]` fields on `NormalizedInvocation` / `NormalizedInvocationInput` flip from `| None = None` to non-null default `[]`.

**Architecture:** New `NormalizedInvocation.accessed_files: list[AIAccessedFile] = Field(default_factory=list)`. Bedrock-side `bedrock/converse_attachments.py::extract_converse_attachments` (async, owns S3 fetches) populates it for Converse document/image blocks. Shared `shared/normalize/finalize.py::finalize` wraps the vendor-agnostic tool-result extractor (`shared/normalize/normalized/tool_results.py::extract_tool_result_files`) and appends to `normalized.accessed_files`. `shared/events.py::build_event` just reads. Old `shared/s3.py` moves verbatim to `bedrock/s3.py`.

**Tech Stack:** Python 3.13, pydantic v2, `_LenientModel` (`extra="ignore"`), uv workspace, pytest with `asyncio_mode = "auto"`, ruff, ty. `@yaml_pytest()` decorator (`shared/src/slashid_ai_forwarder_core/testing.py`) parametrizes tests from `<test-file-dir>/<test-function-name>.yaml` with type-annotation coercion.

**Design doc:** `docs/superpowers/specs/2026-09-04-accessed-files-canonicalization.md` (and mirrored at `/home/paulo/.claude/plans/2026-09-04-accessed-files-canonicalization.md`).

**Test-run convention:** the workspace-root `pyproject.toml` has no pytest config, so `uv run --project shared pytest` from root breaks asyncio mode. Always `cd shared && uv run pytest ...` or `cd bedrock && uv run pytest ...`.

---

## File Structure

Post-refactor:

**Shared (`shared/src/slashid_ai_forwarder_core/`):**

- `events.py` — wire models + `build_event`. Loses `_accessed_files`, `_READ_TOOLS`, `_ToolSpec`, `_extract_otel` + helpers, `_iter_content_blocks` (already unused after Phase 2), `_OTEL_KEY`, `_TRACE_ID_HEX`, `_SPAN_ID_HEX`, `_OTEL_MARKER`, `_otel_from_dict`, `_otel_from_text`. `_used_tools` imports `_extract_otel` from `normalize/normalized/otel.py`. `build_event` reads `normalized.accessed_files` and assigns to wire event with `or None` at the boundary.
- `normalize/normalized/types.py` — canonical types. `NormalizedInvocation` gains `accessed_files`. All list fields flip to non-null default `[]`.
- `normalize/normalized/otel.py` — NEW. Leaf module. `_extract_otel(content) -> tuple[str | None, str | None]` and helpers. Imports nothing internal.
- `normalize/normalized/tool_results.py` — NEW. `extract_tool_result_files(messages, *, include_raw_content, max_content_size) -> list[AIAccessedFile]`. Owns `_READ_TOOLS`, `_ToolSpec`. Imports `_extract_otel` from `otel.py`, `AIAccessedFile` from `events.py`, `strip_cat_n`/`truncate_middle` from `content_utils.py`.
- `normalize/finalize.py` — NEW. `finalize(normalized, *, include_raw_content, max_content_size) -> NormalizedInvocation`. Calls `extract_tool_result_files` and extends `normalized.accessed_files`.
- `s3.py` — DELETED. Moved to `bedrock/src/slashid_bedrock_forwarder/s3.py`.
- `pyproject.toml` — drops `aioboto3` dep.

**Bedrock (`bedrock/src/slashid_bedrock_forwarder/`):**

- `s3.py` — NEW (verbatim move from `shared/`). `resolve_offloaded_bodies`, `fetch_offloaded_body`, `_resolve_s3_attachment`, `MAX_PARALLEL_FETCHES`, `MAX_FETCH_BYTES`.
- `converse_attachments.py` — NEW. `extract_converse_attachments(record, *, include_raw_content, max_content_size) -> list[AIAccessedFile]`. Owns the `_DOC_MIME` map, `_mime_from_name`, inline base64 decode, S3 attachment resolution.
- `handler.py::_run` — composes `resolve_offloaded_bodies` → `normalize_record` → `extract_converse_attachments` → `finalize` → `build_event`.
- `pyproject.toml` — adds `aioboto3` dep; version bumps `0.1.2` → `0.1.3`.

**Tests:**

- `shared/tests/normalize/normalized/test_otel.py` — NEW.
- `shared/tests/normalize/normalized/test_tool_results.py` + `.yaml` — NEW.
- `shared/tests/normalize/test_finalize.py` — NEW (one dir up, mirroring src).
- `shared/tests/test_events.py` — attachment-focused `test_accessed_files_*` cases (14) migrated out; tool-result-focused (6) subsumed by `test_tool_results.py`; 1 cross-path migrated to `bedrock/tests/test_handler.py`. Remaining tests updated to assert on `event.accessed_files` from the composed pipeline (or on `NormalizedInvocation` directly for canonical-shape tests).
- `shared/tests/test_s3.py` — DELETED. Moved to `bedrock/tests/test_s3.py`.
- `bedrock/tests/test_s3.py` — NEW (verbatim move).
- `bedrock/tests/test_converse_attachments.py` — NEW. 14 attachment-focused tests migrated from shared.
- `bedrock/tests/test_handler.py` — extended with 1 end-to-end test covering the composed pipeline + the cross-path dedup case.

---

## Chunk 1: Canonical type field + list-default drive-by

New `NormalizedInvocation.accessed_files` field + flip all `list[...]` fields on `NormalizedInvocation` / `NormalizedInvocationInput` to non-null default `[]`. Update vendor `_to_input` / `_request_to_input` construction sites to drop `or None`. Update `shared/events.py::_used_tools` and `build_event` to drop `or []` guards on canonical reads (keep `or None` at wire-event assignment). Extend round-trip test.

No populators for `accessed_files` yet. No consumers yet. Just plumbing.

**Files:**

- Modify: `shared/src/slashid_ai_forwarder_core/normalize/normalized/types.py`
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/converse/normalize.py`
- Modify: `shared/src/slashid_ai_forwarder_core/normalize/anthropic/normalize.py`
- Modify: `shared/src/slashid_ai_forwarder_core/events.py`
- Modify: `shared/tests/normalize/normalized/test_types_round_trip.py`

### Task 1.1: Flip type defaults in `types.py`

- [ ] **Step 1: Read the current `types.py` for exact placement**

Run: `sed -n '1,120p' shared/src/slashid_ai_forwarder_core/normalize/normalized/types.py`
Expected: sees `NormalizedInvocationInput` with `messages / tools_declared / tool_servers` typed `list[...] | None = None`, and `NormalizedInvocation` with `tokens / input / output` (no `accessed_files` field).

- [ ] **Step 2: Add `AIAccessedFile` to the existing `from ...events import ...` line**

Edit `shared/src/slashid_ai_forwarder_core/normalize/normalized/types.py` — extend the existing `from ...events import (...)` block to include `AIAccessedFile`.

Result:

```python
from ...events import AIAccessedFile, AIInvocationTokens, AIStopReason, AITool, AIToolServer
```

- [ ] **Step 3: Flip `NormalizedInvocationInput` list defaults**

Edit `NormalizedInvocationInput` — replace all three `list[...] | None = None` with `list[...] = Field(default_factory=list)`:

```python
class NormalizedInvocationInput(_LenientModel):
    """Canonical input-side shape — the request body's content-relevant fields.

    Non-content fields (temperature, max_tokens, top_p, stream, etc.) are
    deliberately excluded: they're vendor-specific settings, not content.
    Excluding them means "same conversation with different sampling
    parameters" hashes to the same input — a feature, not a bug.

    List fields default to ``[]`` (not ``None``) — populators always run,
    so there's no meaningful "unpopulated" state. Consumers can traverse
    without an ``or []`` guard.
    """

    messages: list[NormalizedMessage] = Field(default_factory=list)
    tools_declared: list[AITool] = Field(default_factory=list)
    tool_servers: list[AIToolServer] = Field(default_factory=list)
```

- [ ] **Step 4: Add `accessed_files` on `NormalizedInvocation`**

Same file, extend `NormalizedInvocation`:

```python
class NormalizedInvocation(_LenientModel):
    """Full canonical shape for one AI invocation.

    The sub-model split (``input`` / ``output`` / ``tokens``) mirrors the
    wire event's shape ... an empty ``NormalizedInvocation()`` is the
    ``parsed_as="unknown"`` fallthrough — every field defaults so no
    vendor is needed to construct one.
    """

    tokens: AIInvocationTokens = Field(default_factory=AIInvocationTokens)
    input: NormalizedInvocationInput = Field(default_factory=NormalizedInvocationInput)
    output: NormalizedInvocationOutput = Field(default_factory=NormalizedInvocationOutput)
    accessed_files: list[AIAccessedFile] = Field(default_factory=list)
```

- [ ] **Step 5: Verify types.py compiles**

Run: `uv run --project shared python -c "from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation; n = NormalizedInvocation(); print(n.accessed_files, n.input.messages, n.input.tools_declared, n.input.tool_servers)"`
Expected: `[] [] [] []`

### Task 1.2: Drop `or None` in vendor `_to_input` / `_request_to_input`

- [ ] **Step 1: Grep for the sites**

Run: `grep -n "messages or None\|tools_declared or None\|tool_servers or None" shared/src/slashid_ai_forwarder_core/normalize/`
Expected: 6 matches (3 in `converse/normalize.py`, 3 in `anthropic/normalize.py`).

- [ ] **Step 2: Rewrite `converse/normalize.py::_to_input` construction**

Find the `return NormalizedInvocationInput(...)` call at the end of `_to_input(request)`. Replace:

```python
    return NormalizedInvocationInput(
        messages=messages or None,
        tools_declared=tools_declared or None,
        tool_servers=tool_servers or None,
    )
```

with:

```python
    return NormalizedInvocationInput(
        messages=messages,
        tools_declared=tools_declared,
        tool_servers=tool_servers,
    )
```

- [ ] **Step 3: Rewrite `anthropic/normalize.py::_request_to_input` construction**

Same pattern in `_request_to_input`. Drop the three `or None`s.

### Task 1.3: Drop `or []` in `shared/events.py::_used_tools`

- [ ] **Step 1: Locate the `or []` sites in `_used_tools`**

Run: `grep -n "or \[\]" shared/src/slashid_ai_forwarder_core/events.py`

- [ ] **Step 2: Rewrite `_used_tools`**

At the top of `_used_tools`, the current code reads:

```python
    input_messages = (normalized.input.messages or []) if normalized.input else []
    if not input_messages:
        return []

    tools = normalized.input.tools_declared or []
    servers = normalized.input.tool_servers or []
```

Replace with:

```python
    input_messages = normalized.input.messages
    if not input_messages:
        return []

    tools = normalized.input.tools_declared
    servers = normalized.input.tool_servers
```

(`normalized.input` itself is always populated — `NormalizedInvocation.input` has a default factory.)

### Task 1.4: Drop `or []` in `build_event`

- [ ] **Step 1: Locate the corresponding sites in `build_event`**

Run: `grep -n "normalized.input.tool_servers\|normalized.input.tools_declared" shared/src/slashid_ai_forwarder_core/events.py`

- [ ] **Step 2: Rewrite the `build_event` local bindings**

Current:

```python
    servers = normalized.input.tool_servers or []
    tools = normalized.input.tools_declared or []
    used = _used_tools(normalized)
```

Replace with:

```python
    servers = normalized.input.tool_servers
    tools = normalized.input.tools_declared
    used = _used_tools(normalized)
```

Wire-event assembly keeps `or None`:

```python
        available_tool_servers=servers or None,
        available_tools=tools or None,
        used_tools=used or None,
```

(unchanged — verify these `or None` guards are still in the `AIInvocationObservedV1(...)` construction call).

### Task 1.5: Round-trip tests for the new field + defaults

- [ ] **Step 1: Read the current `test_types_round_trip.py`**

Run: `wc -l shared/tests/normalize/normalized/test_types_round_trip.py`
Expected: ~110 lines.

- [ ] **Step 2: Extend with new tests**

Append to `shared/tests/normalize/normalized/test_types_round_trip.py`:

```python
def test_normalized_invocation_input_list_defaults() -> None:
    """List fields on NormalizedInvocationInput default to [] not None."""
    i = NormalizedInvocationInput()
    assert i.messages == []
    assert i.tools_declared == []
    assert i.tool_servers == []


def test_normalized_invocation_accessed_files_default() -> None:
    """accessed_files defaults to [] and can be extended in-place."""
    from slashid_ai_forwarder_core.events import AIAccessedFile
    n = NormalizedInvocation()
    assert n.accessed_files == []
    n.accessed_files.append(AIAccessedFile(name="/tmp/x"))
    assert len(n.accessed_files) == 1


def test_normalized_invocation_accessed_files_round_trip() -> None:
    """model_dump / model_validate preserves accessed_files."""
    from slashid_ai_forwarder_core.events import AIAccessedFile
    n = NormalizedInvocation(
        accessed_files=[AIAccessedFile(name="/tmp/x", byte_length=42)]
    )
    dumped = n.model_dump(mode="json", exclude_none=True)
    reparsed = NormalizedInvocation.model_validate(dumped)
    assert reparsed.accessed_files == n.accessed_files
```

- [ ] **Step 3: Run the extended round-trip suite**

Run: `cd shared && uv run pytest tests/normalize/normalized/test_types_round_trip.py -v`
Expected: previous tests + 3 new tests all pass.

### Task 1.6: Full-suite regression + commit

- [ ] **Step 1: Full shared + bedrock suites**

Run: `(cd shared && uv run pytest -q) && (cd bedrock && uv run pytest -q)`
Expected: all pass. Any test that asserted `normalized.input.messages is None` (etc.) fails; grep for and update those.

- [ ] **Step 2: ty + ruff + format**

Run: `uv run ty check && uv run ruff check shared/ bedrock/ && (cd shared && uv run ruff format --check .) && (cd bedrock && uv run ruff format --check .)`
Expected: all clean.

- [ ] **Step 3: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/normalized/types.py \
        shared/src/slashid_ai_forwarder_core/normalize/converse/normalize.py \
        shared/src/slashid_ai_forwarder_core/normalize/anthropic/normalize.py \
        shared/src/slashid_ai_forwarder_core/events.py \
        shared/tests/normalize/normalized/test_types_round_trip.py
git commit -m "$(cat <<'EOF'
feat(normalize): add NormalizedInvocation.accessed_files + non-null-default lists

- New: NormalizedInvocation.accessed_files: list[AIAccessedFile] = Field(default_factory=list).
- Drive-by: flip NormalizedInvocationInput.{messages, tools_declared, tool_servers}
  from `list[...] | None = None` to `list[...] = Field(default_factory=list)`.

Consumers currently guard every read with `or []` because these fields default
to None; populators always run, so there's no meaningful "unpopulated" state.
Non-null default drops the guards. Wire-model boundary keeps `or None` at
construction so exclude_none=True still omits empty lists on the wire — no
wire behaviour change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Chunk 2: Shared OTel leaf + tool-result extractor

New leaf module `otel.py` (`_extract_otel` + helpers). New `tool_results.py` (`extract_tool_result_files` + `_READ_TOOLS` + `_ToolSpec`). `_used_tools` in shared events.py imports `_extract_otel` from `otel.py` instead of defining it inline.

**Files:**

- Create: `shared/src/slashid_ai_forwarder_core/normalize/normalized/otel.py`
- Create: `shared/src/slashid_ai_forwarder_core/normalize/normalized/tool_results.py`
- Modify: `shared/src/slashid_ai_forwarder_core/events.py`
- Create: `shared/tests/normalize/normalized/test_otel.py`
- Create: `shared/tests/normalize/normalized/test_tool_results.py`
- Create: `shared/tests/normalize/normalized/test_tool_results.yaml`

### Task 2.1: `otel.py` leaf module

- [ ] **Step 1: Read the current `_extract_otel` + helpers**

Run: `grep -n "^_OTEL\|^_TRACE\|^_SPAN\|^def _otel\|^def _extract_otel\|^_OtelCtx" shared/src/slashid_ai_forwarder_core/events.py`
Familiarize with the block being lifted.

- [ ] **Step 2: Create `otel.py` with the extracted code**

Create `shared/src/slashid_ai_forwarder_core/normalize/normalized/otel.py`:

```python
"""OpenTelemetry trace context extraction from MCP tool-result content.

MCP servers running mcp-gate-demo's ``CorrelationIdMiddleware`` echo the
OTel trace/span context back on every tool result. Wire shape:

    {"$opentelemetry": {"trace_id": "<32 hex>", "span_id": "<16 hex>"}}

Primary carrier is MCP ``structuredContent``. Success path preserves it —
reaches Bedrock Converse either as a native ``{json: {...}}`` block or, when
the client stringifies, a JSON-encoded ``{text: "..."}`` block. Error path:
some MCP clients (Claude Code on Bedrock) drop structured content entirely
and forward only the text message, so the middleware also embeds
``[trace_id=<32 hex> span_id=<16 hex>]`` as a trailing text marker.

This leaf module holds the extraction; both
``shared/events.py::_used_tools`` and
``shared/normalize/normalized/tool_results.py::extract_tool_result_files``
import from here. Leaf-only avoids the ``events.py`` ↔ ``tool_results.py``
cycle that would arise if OTel lived in either.
"""

from __future__ import annotations

import json
import re
from typing import Any

_OTEL_KEY = "$opentelemetry"
_TRACE_ID_HEX = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_SPAN_ID_HEX = re.compile(r"^[0-9a-f]{16}$", re.IGNORECASE)
_OTEL_MARKER = re.compile(
    r"\[trace_id=([0-9a-f]{32})\s+span_id=([0-9a-f]{16})\]",
    re.IGNORECASE,
)

_OtelCtx = tuple[str | None, str | None]  # (trace_id, span_id)


def _otel_from_dict(d: Any) -> _OtelCtx:
    """Pull ``$opentelemetry.{trace_id,span_id}`` from a decoded structured-content dict."""
    if not isinstance(d, dict):
        return (None, None)
    otel = d.get(_OTEL_KEY)
    if not isinstance(otel, dict):
        return (None, None)
    raw_t = otel.get("trace_id")
    raw_s = otel.get("span_id")
    trace_id = raw_t.lower() if isinstance(raw_t, str) and _TRACE_ID_HEX.match(raw_t) else None
    span_id = raw_s.lower() if isinstance(raw_s, str) and _SPAN_ID_HEX.match(raw_s) else None
    return (trace_id, span_id)


def _otel_from_text(text: str) -> _OtelCtx:
    """Pull OTel context from a text block via either a JSON envelope or the
    ``[trace_id=<hex> span_id=<hex>]`` marker (the fallback carrier used on
    error paths where the client drops structuredContent).
    """
    stripped = text.strip()
    if stripped and stripped[0] in "{[":
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
        else:
            ctx = _otel_from_dict(parsed)
            if ctx[0]:
                return ctx
    m = _OTEL_MARKER.search(text)
    if m:
        return (m.group(1).lower(), m.group(2).lower())
    return (None, None)


def extract_otel(content: Any) -> _OtelCtx:
    """Pull the MCP ``$opentelemetry`` context from a tool_result payload.

    Handles both the Anthropic tool_result shape (string or list of blocks)
    and the Converse toolResult shape (list of ``{text}`` / ``{json}`` /
    etc. blocks). Returns ``(None, None)`` when the marker is absent.
    """
    if content is None:
        return (None, None)
    if isinstance(content, str):
        return _otel_from_text(content)
    if isinstance(content, dict):
        return _otel_from_dict(content)
    if not isinstance(content, list):
        return (None, None)
    for block in content:
        if not isinstance(block, dict):
            continue
        if isinstance(block.get("json"), dict):
            ctx = _otel_from_dict(block["json"])
            if ctx[0]:
                return ctx
        text = block.get("text")
        if isinstance(text, str):
            ctx = _otel_from_text(text)
            if ctx[0]:
                return ctx
    return (None, None)
```

Note: renamed `_extract_otel` → `extract_otel` (public, single leading underscore removed) since it's now imported across module boundaries. Other names (`_otel_from_dict`, `_otel_from_text`) stay private helpers.

- [ ] **Step 3: Write the unit test**

Create `shared/tests/normalize/normalized/test_otel.py`:

```python
"""Unit tests for extract_otel — pulls OTel context from MCP tool_result content."""

from __future__ import annotations

import json

import pytest

from slashid_ai_forwarder_core.normalize.normalized.otel import extract_otel

_TRACE = "a" * 32
_SPAN = "1" * 16


def test_extract_otel_none() -> None:
    assert extract_otel(None) == (None, None)


def test_extract_otel_empty_string() -> None:
    assert extract_otel("") == (None, None)


def test_extract_otel_json_string_envelope() -> None:
    envelope = json.dumps({"$opentelemetry": {"trace_id": _TRACE, "span_id": _SPAN}})
    assert extract_otel(envelope) == (_TRACE, _SPAN)


def test_extract_otel_text_marker() -> None:
    text = f"error occurred\n[trace_id={_TRACE} span_id={_SPAN}]"
    assert extract_otel(text) == (_TRACE, _SPAN)


def test_extract_otel_converse_json_block() -> None:
    """Converse content array with a {json: {...}} block."""
    content = [{"json": {"$opentelemetry": {"trace_id": _TRACE, "span_id": _SPAN}}}]
    assert extract_otel(content) == (_TRACE, _SPAN)


def test_extract_otel_converse_text_block_with_json_envelope() -> None:
    """Some clients stringify structuredContent as a {text: '<json>'} block."""
    envelope = json.dumps({"$opentelemetry": {"trace_id": _TRACE, "span_id": _SPAN}})
    content = [{"text": envelope}]
    assert extract_otel(content) == (_TRACE, _SPAN)


def test_extract_otel_converse_text_block_with_marker() -> None:
    """Error paths: text block with the [trace_id=… span_id=…] marker."""
    text = f"tool failed [trace_id={_TRACE} span_id={_SPAN}]"
    content = [{"text": text}]
    assert extract_otel(content) == (_TRACE, _SPAN)


def test_extract_otel_ignores_invalid_hex_lengths() -> None:
    """trace_id must be exactly 32 hex chars, span_id exactly 16."""
    envelope = json.dumps({"$opentelemetry": {"trace_id": "abc", "span_id": "def"}})
    assert extract_otel(envelope) == (None, None)


def test_extract_otel_lowercases_hex() -> None:
    """Uppercase hex normalises to lowercase."""
    envelope = json.dumps({"$opentelemetry": {"trace_id": _TRACE.upper(), "span_id": _SPAN.upper()}})
    assert extract_otel(envelope) == (_TRACE, _SPAN)


def test_extract_otel_missing_key() -> None:
    """Structured content without the $opentelemetry key returns (None, None)."""
    content = [{"json": {"flow": "gate_svid"}}]
    assert extract_otel(content) == (None, None)
```

- [ ] **Step 4: Run the test**

Run: `cd shared && uv run pytest tests/normalize/normalized/test_otel.py -v`
Expected: 10 tests pass.

### Task 2.2: `tool_results.py` extractor + `_READ_TOOLS`

- [ ] **Step 1: Create `tool_results.py`**

Create `shared/src/slashid_ai_forwarder_core/normalize/normalized/tool_results.py`:

```python
"""Vendor-agnostic Read-tool → AIAccessedFile extraction over canonical messages.

Walks ``NormalizedInvocation.input.messages`` for tool_use/tool_result
pairs matching the ``_READ_TOOLS`` table (Claude Code Read, OpenCode /
Amazon Q / Gemini CLI ReadFile / read_file / view_file, Claude
computer-use text-editor tool). For each match: hashes the returned
bytes (with per-tool cleanup — e.g. ``strip_cat_n`` for Claude Code's
line-number prefix), builds an AIAccessedFile keyed by the tool's
``file_path`` / ``path`` argument.

Skips pairs where ``tool_is_error`` is True — on error paths the
tool_result content is an error-message body, not file bytes, and
hashing it would attribute the error string to the file path. The
tool-failure signal is preserved on the corresponding ``used_tools``
entry (see ``events.py::_used_tools``).

Only fresh-region tool_results count (after the last assistant message);
earlier tool_results were already reported on prior invocation events.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue

from ...content_utils import strip_cat_n, truncate_middle
from ...events import AIAccessedFile
from .otel import extract_otel
from .types import NormalizedMessage


class _ToolSpec(BaseModel):
    """Per-tool declaration: which input field carries the file path, and
    an optional cleanup function to apply to the returned content before
    hashing."""

    model_config = ConfigDict(frozen=True)
    field_name: str
    cleanup: Callable[[str], str] | None = None


# Canonical reference: https://docs.anthropic.com/en/docs/claude-code/tools
_READ_TOOLS: dict[str, _ToolSpec] = {
    "Read": _ToolSpec(field_name="file_path", cleanup=strip_cat_n),  # Claude Code
    "ReadFile": _ToolSpec(field_name="path"),  # OpenCode, Amazon Q Developer, Gemini CLI
    "read_file": _ToolSpec(field_name="path"),  # snake_case variants
    "view_file": _ToolSpec(field_name="path"),  # some agents
    "str_replace_based_edit_tool": _ToolSpec(
        field_name="path"
    ),  # Claude computer-use text editor view
}


def extract_tool_result_files(
    messages: list[NormalizedMessage],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    """Walk canonical messages for _READ_TOOLS-matching tool_use/tool_result pairs.

    Returns one AIAccessedFile per unique (path, sha256) — dedup within a
    single call. Callers (typically ``finalize``) append to
    ``normalized.accessed_files``. See module docstring for the is_error
    guard and fresh-region rule.
    """
    if not messages:
        return []

    # 1. tool_use_id → (raw_tool_name, tool_input) — from any assistant tool_use block.
    tool_use_by_id: dict[str, tuple[str, JsonValue]] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        for block in msg.content:
            if block.kind == "tool_use" and block.tool_use_id and block.tool_name:
                tool_use_by_id[block.tool_use_id] = (block.tool_name, block.tool_input)

    # 2. Fresh region: everything after the last assistant message.
    last_assistant = max(
        (i for i, m in enumerate(messages) if m.role == "assistant"),
        default=-1,
    )

    # 3. For each fresh tool_result, correlate + hash.
    # Dedup key is (name, sha256) — matches Phase 1 behaviour. Edge case:
    # if content bytes couldn't be derived (empty tool_output, list of
    # non-text blocks), sha256 falls to None, and multiple different
    # tool_results for the same path collapse to one entry. Rare;
    # matches existing behaviour; live-safe.
    out: list[AIAccessedFile] = []
    seen: set[tuple[str, str | None]] = set()
    for msg in messages[last_assistant + 1 :]:
        for block in msg.content:
            if block.kind != "tool_result" or not block.tool_use_id:
                continue
            if block.tool_is_error:
                # Error paths: content is an error message, not file bytes.
                # Skip — hashing would attribute the error to the file path.
                continue

            pair = tool_use_by_id.get(block.tool_use_id)
            if not pair:
                continue
            tool_name, tool_input = pair
            spec = _READ_TOOLS.get(tool_name)
            if not spec:
                continue

            path = _get_path_field(tool_input, spec.field_name)
            if not path:
                continue

            content_bytes = _bytes_from_tool_output(block.tool_output, spec.cleanup)
            file = _build_accessed_file(
                name=path,
                media_type=_mime_from_name(path),
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


def _get_path_field(tool_input: JsonValue, field_name: str) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    val = tool_input.get(field_name)
    return val if isinstance(val, str) and val else None


def _bytes_from_tool_output(
    tool_output: JsonValue, cleanup: Callable[[str], str] | None
) -> bytes | None:
    """Extract the raw bytes to hash from a tool_result's content payload.

    Anthropic shape: string or list of blocks. Converse shape: list of
    ``{text}`` / ``{json}`` etc. blocks. String content is used directly;
    list content concatenates text blocks. Cleanup (e.g. ``strip_cat_n``)
    is applied to the raw string before encoding.
    """
    if isinstance(tool_output, str):
        text = tool_output
    elif isinstance(tool_output, list):
        text = "".join(b.get("text", "") for b in tool_output if isinstance(b, dict))
    else:
        return None
    if not text:
        return None
    if cleanup is not None:
        text = cleanup(text)
    return text.encode()


def _mime_from_name(name: str | None) -> str | None:
    if not name:
        return None
    import mimetypes

    mt, _ = mimetypes.guess_type(name)
    return mt or None


def _build_accessed_file(
    *,
    name: str,
    media_type: str | None,
    content_bytes: bytes | None,
    include_raw_content: bool,
    max_content_size: int,
) -> AIAccessedFile:
    """Assemble an AIAccessedFile with hashes / byte_length / optional redacted_content."""
    if content_bytes is not None:
        content_hashes: dict[str, str] | None = {
            "sha256": hashlib.sha256(content_bytes).hexdigest(),
            "sha1": hashlib.sha1(content_bytes).hexdigest(),
            "md5": hashlib.md5(content_bytes).hexdigest(),
        }
        byte_length = len(content_bytes)
    else:
        content_hashes = None
        byte_length = None
    redacted = None
    if include_raw_content and content_bytes is not None:
        redacted = truncate_middle(content_bytes.decode(errors="replace"), max_content_size)
    return AIAccessedFile(
        name=name,
        content_hashes=content_hashes,
        media_type=media_type,
        byte_length=byte_length,
        redacted_content=redacted,
    )
```

- [ ] **Step 2: Create the YAML fixture**

Create `shared/tests/normalize/normalized/test_tool_results.yaml`:

```yaml
id: read_cat_n_content_hashes_raw_bytes
messages:
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_1
        tool_name: Read
        tool_input: {file_path: "/repo/main.py"}
        tool_is_error: false
  - role: user
    content:
      - kind: tool_result
        tool_use_id: tu_1
        tool_output: "     1\tline1\n     2\tline2\n"
        tool_is_error: false
expected:
  - name: "/repo/main.py"
    byte_length: 12   # "line1\nline2\n" = 12 bytes
    media_type: text/x-python
---
id: readfile_plain_content
messages:
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_2
        tool_name: ReadFile
        tool_input: {path: "/repo/config.json"}
        tool_is_error: false
  - role: user
    content:
      - kind: tool_result
        tool_use_id: tu_2
        tool_output: "{}"
        tool_is_error: false
expected:
  - name: "/repo/config.json"
    byte_length: 2
    media_type: application/json
---
id: is_error_skips_entry
messages:
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_err
        tool_name: Read
        tool_input: {file_path: "/etc/hostname"}
        tool_is_error: false
  - role: user
    content:
      - kind: tool_result
        tool_use_id: tu_err
        tool_output: "permission denied"
        tool_is_error: true
expected: []
---
id: unknown_tool_ignored
messages:
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_bash
        tool_name: Bash
        tool_input: {command: "ls"}
        tool_is_error: false
  - role: user
    content:
      - kind: tool_result
        tool_use_id: tu_bash
        tool_output: "total 8\n..."
        tool_is_error: false
expected: []
---
id: tool_use_without_result_defers
messages:
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_pending
        tool_name: Read
        tool_input: {file_path: "/tmp/pending.txt"}
        tool_is_error: false
expected: []
---
id: converse_list_content_concatenated
messages:
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_conv
        tool_name: Read
        tool_input: {file_path: "/tmp/multi.txt"}
        tool_is_error: false
  - role: user
    content:
      - kind: tool_result
        tool_use_id: tu_conv
        tool_output:
          - {text: "     1\tfoo\n"}
          - {text: "     2\tbar\n"}
        tool_is_error: false
expected:
  - name: "/tmp/multi.txt"
    byte_length: 8   # "foo\nbar\n"
    media_type: text/plain
---
id: tool_result_only_last_turn
messages:
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_old
        tool_name: Read
        tool_input: {file_path: "/tmp/old.txt"}
        tool_is_error: false
  - role: user
    content:
      - kind: tool_result
        tool_use_id: tu_old
        tool_output: "old content"
        tool_is_error: false
  - role: assistant
    content:
      - kind: tool_use
        tool_use_id: tu_new
        tool_name: Read
        tool_input: {file_path: "/tmp/new.txt"}
        tool_is_error: false
  - role: user
    content:
      - kind: tool_result
        tool_use_id: tu_new
        tool_output: "new content"
        tool_is_error: false
expected:
  - name: "/tmp/new.txt"
    byte_length: 11
    media_type: text/plain
```

- [ ] **Step 3: Create the parametrized test module**

Create `shared/tests/normalize/normalized/test_tool_results.py`:

```python
"""YAML-driven tests for extract_tool_result_files."""

from __future__ import annotations

import hashlib

from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.normalized.tool_results import (
    extract_tool_result_files,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedMessage
from slashid_ai_forwarder_core.testing import yaml_pytest


@yaml_pytest()
def test_extract_tool_result_files(
    messages: list[NormalizedMessage],
    expected: list[AIAccessedFile],
) -> None:
    """Compare only name/byte_length/media_type — content_hashes are derived
    from the bytes and asserted separately for the non-error cases."""
    out = extract_tool_result_files(
        messages, include_raw_content=False, max_content_size=100_000
    )
    assert len(out) == len(expected)
    for got, want in zip(out, expected, strict=True):
        assert got.name == want.name
        assert got.byte_length == want.byte_length
        assert got.media_type == want.media_type
        if want.byte_length is None:
            assert got.content_hashes is None
        else:
            assert got.content_hashes is not None
            assert "sha256" in got.content_hashes


def test_read_cat_n_hash_matches_stripped_bytes() -> None:
    """The Read cleanup strips cat-n prefixes before hashing."""
    from slashid_ai_forwarder_core.events import AIAccessedFile
    msg_asst = NormalizedMessage.model_validate({
        "role": "assistant",
        "content": [{
            "kind": "tool_use",
            "tool_use_id": "tu_1",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x.py"},
            "tool_is_error": False,
        }],
    })
    msg_user = NormalizedMessage.model_validate({
        "role": "user",
        "content": [{
            "kind": "tool_result",
            "tool_use_id": "tu_1",
            "tool_output": "     1\thello\n",
            "tool_is_error": False,
        }],
    })
    out = extract_tool_result_files(
        [msg_asst, msg_user], include_raw_content=False, max_content_size=100_000
    )
    assert len(out) == 1
    expected = hashlib.sha256(b"hello\n").hexdigest()
    assert out[0].content_hashes["sha256"] == expected


def test_extract_tool_result_files_include_raw_populates_redacted() -> None:
    msg_asst = NormalizedMessage.model_validate({
        "role": "assistant",
        "content": [{
            "kind": "tool_use",
            "tool_use_id": "tu_1",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
            "tool_is_error": False,
        }],
    })
    msg_user = NormalizedMessage.model_validate({
        "role": "user",
        "content": [{
            "kind": "tool_result",
            "tool_use_id": "tu_1",
            "tool_output": "secret",
            "tool_is_error": False,
        }],
    })
    out = extract_tool_result_files(
        [msg_asst, msg_user], include_raw_content=True, max_content_size=100_000
    )
    assert out[0].redacted_content == "secret"
```

- [ ] **Step 4: Run the tool_results tests**

Run: `cd shared && uv run pytest tests/normalize/normalized/test_tool_results.py -v`
Expected: 7 parametrized cases + 2 Python tests = 9 tests pass.

### Task 2.3: Update `_used_tools` to import from `otel.py`

- [ ] **Step 1: Import `extract_otel` at the top of `events.py`**

Add near the other internal imports:

```python
from .normalize.normalized.otel import extract_otel
```

- [ ] **Step 2: Delete the inline OTel code from `events.py`**

Delete these definitions (still needed by `_used_tools` — but the import now handles it):

- `_OTEL_KEY`, `_TRACE_ID_HEX`, `_SPAN_ID_HEX`, `_OTEL_MARKER` module constants
- `_OtelCtx` type alias
- `_otel_from_dict`, `_otel_from_text`, `_extract_otel` functions
- The comment block explaining `$opentelemetry` (also moves to `otel.py`)

- [ ] **Step 3: Update `_used_tools`'s call site**

Change `_extract_otel(block.tool_output)` → `extract_otel(block.tool_output)`.

- [ ] **Step 4: Verify no lingering `_extract_otel` refs**

Run: `grep -rn "_extract_otel\|_otel_from_dict\|_otel_from_text\|_OTEL_KEY\|_TRACE_ID_HEX\|_SPAN_ID_HEX\|_OTEL_MARKER" shared/src/`
Expected: no matches (all moved to `otel.py` as `extract_otel` / private helpers).

- [ ] **Step 5: Run full shared suite**

Run: `cd shared && uv run pytest -q`
Expected: all green. `_used_tools` still works — same function body, just calling `extract_otel` from the leaf.

### Task 2.4: Commit Chunk 2

- [ ] **Step 1: Ruff + ty + format**

Run: `uv run ty check && uv run ruff check shared/ && (cd shared && uv run ruff format --check .)`
Expected: clean.

- [ ] **Step 2: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/normalized/otel.py \
        shared/src/slashid_ai_forwarder_core/normalize/normalized/tool_results.py \
        shared/src/slashid_ai_forwarder_core/events.py \
        shared/tests/normalize/normalized/test_otel.py \
        shared/tests/normalize/normalized/test_tool_results.py \
        shared/tests/normalize/normalized/test_tool_results.yaml
git commit -m "$(cat <<'EOF'
feat(normalize): add OTel leaf + vendor-agnostic tool-result extractor

- New shared/normalize/normalized/otel.py: leaf module for OTel trace
  context extraction from MCP tool_result content. Renamed
  `_extract_otel` → public `extract_otel` (crosses module boundaries now).
- New shared/normalize/normalized/tool_results.py: extract_tool_result_files
  walks canonical NormalizedInvocation.input.messages for Read-tool
  patterns, hashes the returned bytes, returns AIAccessedFile entries.
  Owns _READ_TOOLS / _ToolSpec. Skips is_error tool_results (bakes in the
  fix from the retired PR #16). Uses extract_otel from the leaf.
- shared/events.py: _used_tools imports extract_otel from the leaf,
  drops the inline definitions.

Leaf module (not co-located with tool_results.py) breaks the would-be
events.py ↔ tool_results.py cycle: both import extract_otel from otel.py,
which imports nothing internal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Chunk 3: Shared `finalize` wrapper

New `shared/normalize/finalize.py` module with a single `finalize(normalized, ...)` post-hook. Called by the bedrock handler after `extract_converse_attachments` populates `normalized.accessed_files`; appends tool-result files.

**Files:**

- Create: `shared/src/slashid_ai_forwarder_core/normalize/finalize.py`
- Create: `shared/tests/normalize/test_finalize.py`

### Task 3.1: `finalize.py`

- [ ] **Step 1: Create the module**

Create `shared/src/slashid_ai_forwarder_core/normalize/finalize.py`:

```python
"""Vendor-agnostic post-processing pass — populates canonical accessed_files
with tool-result-derived entries.

Composes on top of any vendor-side attachment extractor (e.g.
``bedrock.converse_attachments.extract_converse_attachments``): the caller
runs attachment extraction first (writes to ``normalized.accessed_files``),
then calls ``finalize`` to append tool-result-derived entries in a single
pass.

Not idempotent: a second call would re-append tool-result files (the
extractor dedups within its own call but doesn't cross-check against
``normalized.accessed_files``). Callers invoke this exactly once per
invocation, immediately before ``build_event``.
"""

from __future__ import annotations

from .normalized.tool_results import extract_tool_result_files
from .normalized.types import NormalizedInvocation


def finalize(
    normalized: NormalizedInvocation,
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> NormalizedInvocation:
    """Append tool-result files to ``normalized.accessed_files``. Returns the
    same instance (mutation, not clone) for chainable use."""
    tool_files = extract_tool_result_files(
        normalized.input.messages,
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )
    normalized.accessed_files.extend(tool_files)
    return normalized
```

- [ ] **Step 2: Write the tests**

Create `shared/tests/normalize/test_finalize.py`:

```python
"""Tests for finalize — the single-pass tool-result post-hook."""

from __future__ import annotations

import pytest

from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedMessage,
)


def _invocation_with_read(path: str, tool_output: str, *, is_error: bool = False):
    """Build a minimal invocation with one Read/tool_result pair."""
    return NormalizedInvocation(
        input=NormalizedInvocationInput(
            messages=[
                NormalizedMessage(
                    role="assistant",
                    content=[NormalizedContent(
                        kind="tool_use",
                        tool_use_id="tu_1",
                        tool_name="Read",
                        tool_input={"file_path": path},
                    )],
                ),
                NormalizedMessage(
                    role="user",
                    content=[NormalizedContent(
                        kind="tool_result",
                        tool_use_id="tu_1",
                        tool_output=tool_output,
                        tool_is_error=is_error,
                    )],
                ),
            ],
        ),
    )


def test_finalize_empty_invocation() -> None:
    """No messages → no tool-result files. accessed_files stays empty."""
    n = NormalizedInvocation()
    finalize(n, include_raw_content=False, max_content_size=100_000)
    assert n.accessed_files == []


def test_finalize_appends_tool_result_files() -> None:
    n = _invocation_with_read("/tmp/x.txt", "hello world")
    finalize(n, include_raw_content=False, max_content_size=100_000)
    assert len(n.accessed_files) == 1
    assert n.accessed_files[0].name == "/tmp/x.txt"
    assert n.accessed_files[0].byte_length == 11


def test_finalize_skips_is_error() -> None:
    """is_error=True → no accessed_files entry."""
    n = _invocation_with_read("/tmp/x.txt", "permission denied", is_error=True)
    finalize(n, include_raw_content=False, max_content_size=100_000)
    assert n.accessed_files == []


def test_finalize_preserves_pre_existing_entries() -> None:
    """If accessed_files was already populated (e.g. by bedrock-side attachment
    extractor), finalize appends to it, doesn't replace."""
    n = _invocation_with_read("/tmp/x.txt", "hello")
    n.accessed_files.append(AIAccessedFile(name="/attach/pdf", byte_length=1024))
    finalize(n, include_raw_content=False, max_content_size=100_000)
    names = [f.name for f in n.accessed_files]
    assert names == ["/attach/pdf", "/tmp/x.txt"]


def test_finalize_is_not_idempotent() -> None:
    """A second call re-appends the same tool-result files (documented
    non-idempotency — callers invoke exactly once per invocation).

    This test locks in the behaviour so accidental idempotency would fail
    it, forcing a design conversation."""
    n = _invocation_with_read("/tmp/x.txt", "hello")
    finalize(n, include_raw_content=False, max_content_size=100_000)
    finalize(n, include_raw_content=False, max_content_size=100_000)
    assert len(n.accessed_files) == 2
    assert n.accessed_files[0] == n.accessed_files[1]


def test_finalize_returns_same_instance() -> None:
    """Mutation, not clone — the returned object is the same instance."""
    n = NormalizedInvocation()
    result = finalize(n, include_raw_content=False, max_content_size=100_000)
    assert result is n


def test_finalize_include_raw_content_populates_redacted() -> None:
    n = _invocation_with_read("/tmp/x.txt", "secret content")
    finalize(n, include_raw_content=True, max_content_size=100_000)
    assert n.accessed_files[0].redacted_content == "secret content"
```

- [ ] **Step 3: Run the finalize tests**

Run: `cd shared && uv run pytest tests/normalize/test_finalize.py -v`
Expected: 7 tests pass.

### Task 3.2: Commit Chunk 3

- [ ] **Step 1: Full-suite regression**

Run: `(cd shared && uv run pytest -q) && (cd bedrock && uv run pytest -q)`
Expected: all green.

- [ ] **Step 2: Ruff + ty + format**

Run: `uv run ty check && uv run ruff check shared/ bedrock/ && (cd shared && uv run ruff format --check .) && (cd bedrock && uv run ruff format --check .)`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add shared/src/slashid_ai_forwarder_core/normalize/finalize.py \
        shared/tests/normalize/test_finalize.py
git commit -m "$(cat <<'EOF'
feat(normalize): add finalize() single-pass post-hook

Wraps extract_tool_result_files and appends the results to
normalized.accessed_files. Callers invoke exactly once per invocation,
after any vendor-side attachment extractor (e.g. bedrock's
extract_converse_attachments) has populated accessed_files.

Documented non-idempotent: a second call double-appends. Test locks
in the behaviour.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Chunk 4: Atomic switch — bedrock-side extractor + drop shared boto + rewire handler

Big commit. Moves `shared/s3.py` to `bedrock/`, creates `bedrock/converse_attachments.py`, deletes `_accessed_files` and helpers from `shared/events.py`, rewires the handler, drops `aioboto3` from shared, adds it to bedrock, migrates 15 tests.

**Files:**

- Delete: `shared/src/slashid_ai_forwarder_core/s3.py` → `bedrock/src/slashid_bedrock_forwarder/s3.py`
- Delete: `shared/tests/test_s3.py` → `bedrock/tests/test_s3.py`
- Create: `bedrock/src/slashid_bedrock_forwarder/converse_attachments.py`
- Modify: `shared/src/slashid_ai_forwarder_core/events.py` — delete `_accessed_files` and friends
- Modify: `bedrock/src/slashid_bedrock_forwarder/handler.py::_run`
- Modify: `shared/pyproject.toml` — drop `aioboto3`
- Modify: `bedrock/pyproject.toml` — add `aioboto3`
- Modify: `shared/tests/test_events.py` — delete migrated tests
- Create: `bedrock/tests/test_converse_attachments.py`
- Modify: `bedrock/tests/test_handler.py` — end-to-end sanity test

### Task 4.1: Move `s3.py` shared → bedrock

- [ ] **Step 1: `git mv` both files**

Run: `git mv shared/src/slashid_ai_forwarder_core/s3.py bedrock/src/slashid_bedrock_forwarder/s3.py && git mv shared/tests/test_s3.py bedrock/tests/test_s3.py`

- [ ] **Step 2: Verify contents unchanged**

Run: `git diff --stat HEAD`
Expected: only rename entries, no content changes.

### Task 4.2: Create `bedrock/converse_attachments.py`

- [ ] **Step 1: Read the current `_accessed_files` from `events.py` for the extraction logic**

Run: `grep -n "^async def _accessed_files\|^def _accessed_files" shared/src/slashid_ai_forwarder_core/events.py`
Then: `sed -n '<start>,<end>p'` on the range (roughly lines 374–670 in current events.py).

Note the sub-blocks: the setup (`_DOC_MIME` dict inside the function), the `_add`/`_decode_b64`/`_mime_from_name` helpers, the S3-source collection + concurrent resolution, the document/image walk, and the tool-result correlation. The tool-result part goes to `tool_results.py` (Chunk 2). Everything else moves here.

- [ ] **Step 2: Create the module**

Create `bedrock/src/slashid_bedrock_forwarder/converse_attachments.py`:

```python
"""Bedrock Converse document/image attachment extraction → AIAccessedFile.

Walks the raw MIL record's inputBodyJson for ``{document: {...}}`` and
``{image: {...}}`` blocks in messages, decodes inline base64 bytes, or
resolves ``{s3Location: {uri}}`` sources via HeadObject + optional
GetObject (see ``s3.py::_resolve_s3_attachment``). Returns a list of
``AIAccessedFile`` entries for consumption by the shared finalize step
via ``normalized.accessed_files.extend(...)``.

This is vendor-specific — Converse's document/image block shape is
Bedrock-only. Other vendors' equivalents (Vertex ``inlineData``, OpenAI
Responses ``input_image``, etc.) get their own extractors in their own
forwarder subprojects following the same pattern.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
from typing import Any

from slashid_ai_forwarder_core.content_utils import truncate_middle
from slashid_ai_forwarder_core.events import AIAccessedFile

from .s3 import MAX_PARALLEL_FETCHES, _resolve_s3_attachment

# Bedrock Converse format enum → IANA media types.
# Document canonical list:
# https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_DocumentBlock.html
# Image canonical list:
# https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ImageBlock.html
_DOC_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


async def extract_converse_attachments(
    record: dict[str, Any],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    """Walk Converse messages for document/image blocks → AIAccessedFile[].

    S3-sourced attachments are resolved concurrently up-front (bounded by
    ``MAX_PARALLEL_FETCHES``). Inline base64 attachments are decoded
    synchronously as messages are walked. Only fresh-region messages
    count — attachments in prior turns were reported on earlier events.
    """
    body = (record.get("input") or {}).get("inputBodyJson")
    if not isinstance(body, dict):
        return []
    messages = [m for m in (body.get("messages") or []) if isinstance(m, dict)]
    if not messages:
        return []

    last_assistant = max(
        (i for i, m in enumerate(messages) if m.get("role") == "assistant"),
        default=-1,
    )
    fresh_messages = messages[last_assistant + 1 :]

    # Collect all S3 source blocks from fresh messages so we can resolve
    # them concurrently before building the file list.
    s3_sources: list[dict[str, Any]] = []
    for msg in fresh_messages:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            for key in ("document", "image"):
                item = block.get(key)
                if isinstance(item, dict):
                    src = item.get("source") or {}
                    if "s3Location" in src or "s3Uri" in src:
                        s3_sources.append(src)

    if s3_sources:
        sem = asyncio.Semaphore(MAX_PARALLEL_FETCHES)

        async def _guarded(src: dict[str, Any]) -> None:
            async with sem:
                await _resolve_s3_attachment(src, max_content_size=max_content_size)

        await asyncio.gather(*(_guarded(src) for src in s3_sources))

    return _build_files_from_messages(
        fresh_messages,
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )


def _mime_from_name(name: str | None) -> str | None:
    if not name:
        return None
    mt, _ = mimetypes.guess_type(name)
    return mt or None


def _decode_b64(val: Any) -> bytes | None:
    if not val:
        return None
    try:
        return base64.b64decode(val)
    except Exception:
        return None


def _build_files_from_messages(
    fresh_messages: list[dict[str, Any]],
    *,
    include_raw_content: bool,
    max_content_size: int,
) -> list[AIAccessedFile]:
    files: list[AIAccessedFile] = []
    seen: set[tuple[str | None, str | None]] = set()

    def _add(
        name: str | None,
        media_type: str | None,
        raw_bytes: bytes | None,
        length: int | None = None,
        partial_head: bytes | None = None,
        partial_tail: bytes | None = None,
    ) -> None:
        # Full bytes → stable hashes. Partial fetch → no hashes (bytes incomplete).
        content_hashes: dict[str, str] | None
        if raw_bytes is not None:
            content_hashes = {
                "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "sha1": hashlib.sha1(raw_bytes).hexdigest(),
                "md5": hashlib.md5(raw_bytes).hexdigest(),
            }
        else:
            content_hashes = None
        key = (name, content_hashes["sha256"] if content_hashes else None)
        if key in seen:
            return
        seen.add(key)
        redacted: str | None = None
        if include_raw_content:
            if raw_bytes is not None:
                redacted = truncate_middle(raw_bytes.decode(errors="replace"), max_content_size)
            elif partial_head is not None and partial_tail is not None:
                head_str = partial_head.decode(errors="replace")
                tail_str = partial_tail.decode(errors="replace")
                combined = head_str + "…" + tail_str
                redacted = truncate_middle(combined, max_content_size)
        files.append(
            AIAccessedFile(
                name=name,
                content_hashes=content_hashes,
                media_type=media_type,
                byte_length=len(raw_bytes) if raw_bytes is not None else length,
                redacted_content=redacted,
            )
        )

    for msg in fresh_messages:
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue

            if "document" in block:
                doc = block["document"] or {}
                fmt = doc.get("format") or None
                source = doc.get("source") or {}
                name = doc.get("name") or None
                if "bytes" in source:
                    raw_bytes = _decode_b64(source["bytes"])
                    _add(
                        name=name,
                        media_type=(
                            _DOC_MIME.get(fmt, f"application/{fmt}")
                            if fmt
                            else _mime_from_name(name)
                        ),
                        raw_bytes=raw_bytes,
                    )
                else:
                    uri = (source.get("s3Location") or {}).get("uri") or source.get("s3Uri") or None
                    file_name = name or uri
                    mime_hint = _mime_from_name(file_name) or _mime_from_name(uri)
                    if "_resolved_byte_length" not in source:
                        # HEAD failed — emit a stub so callers know the file was referenced.
                        _add(name=file_name, media_type=mime_hint, raw_bytes=None)
                    else:
                        media_type = (
                            _DOC_MIME.get(fmt, f"application/{fmt}")
                            if fmt
                            else source.get("_resolved_content_type") or mime_hint
                        )
                        _add(
                            name=file_name,
                            media_type=media_type,
                            raw_bytes=source.get("_resolved_bytes"),
                            length=source.get("_resolved_byte_length"),
                            partial_head=source.get("_resolved_head_bytes"),
                            partial_tail=source.get("_resolved_tail_bytes"),
                        )

            elif "image" in block:
                img = block["image"] or {}
                fmt = img.get("format") or None
                source = img.get("source") or {}
                if "bytes" in source:
                    raw_bytes = _decode_b64(source["bytes"])
                    _add(
                        name=None,
                        media_type=_DOC_MIME.get(fmt, f"image/{fmt}") if fmt else None,
                        raw_bytes=raw_bytes,
                    )
                else:
                    uri = (source.get("s3Location") or {}).get("uri") or source.get("s3Uri") or None
                    if "_resolved_byte_length" not in source:
                        _add(name=uri, media_type=_mime_from_name(uri), raw_bytes=None)
                    else:
                        media_type = (
                            _DOC_MIME.get(fmt, f"image/{fmt}")
                            if fmt
                            else source.get("_resolved_content_type") or _mime_from_name(uri)
                        )
                        _add(
                            name=uri,
                            media_type=media_type,
                            raw_bytes=source.get("_resolved_bytes"),
                            length=source.get("_resolved_byte_length"),
                            partial_head=source.get("_resolved_head_bytes"),
                            partial_tail=source.get("_resolved_tail_bytes"),
                        )

    return files
```

### Task 4.3: Delete `_accessed_files` and OTel/READ_TOOLS helpers from `events.py`

- [ ] **Step 1: Grep for everything to delete**

Run: `grep -n "^async def _accessed_files\|^def _iter_content_blocks\|^_READ_TOOLS\|^_ToolSpec\|^class _ToolSpec" shared/src/slashid_ai_forwarder_core/events.py`

- [ ] **Step 2: Delete `_accessed_files` function**

Delete the whole `async def _accessed_files(...)` block from `shared/src/slashid_ai_forwarder_core/events.py` (roughly 300 lines).

- [ ] **Step 3: Delete `_READ_TOOLS` + `_ToolSpec` if still present**

Run: `grep -n "_READ_TOOLS\|_ToolSpec\|strip_cat_n" shared/src/slashid_ai_forwarder_core/events.py`

If any remain (they were relocated in Chunk 2 to `tool_results.py`; Chunk 2 already removed the definitions from events.py so this step may be a no-op), delete them here.

- [ ] **Step 4: Delete `_iter_content_blocks`** if it's still around

Run: `grep -n "_iter_content_blocks" shared/src/slashid_ai_forwarder_core/events.py`
If present, delete — it was orphaned after Phase 2 and Chunk 2 may not have removed it.

- [ ] **Step 5: Simplify `build_event`**

The current wire-event construction has:

```python
        accessed_files=await _accessed_files(
            record,
            include_raw_content=include_raw_content,
            max_content_size=max_content_size,
        )
        or None,
```

Replace with:

```python
        accessed_files=normalized.accessed_files or None,
```

- [ ] **Step 6: Verify unused imports get flagged**

Run: `uv run ruff check shared/src/slashid_ai_forwarder_core/events.py`

Expected: ruff flags unused imports (probably `strip_cat_n`, `Callable`, `re` if no other regex remains). Delete each. Re-run until clean.

### Task 4.4: Rewire `bedrock/handler.py::_run`

- [ ] **Step 1: Read current `_run`**

Run: `sed -n '/^async def _run/,/^def /p' bedrock/src/slashid_bedrock_forwarder/handler.py | head -50`

- [ ] **Step 2: Update imports at top of `handler.py`**

Change:

```python
from slashid_ai_forwarder_core.s3 import resolve_offloaded_bodies
```

to:

```python
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
```

Add near the module-local imports:

```python
from .converse_attachments import extract_converse_attachments
from .s3 import resolve_offloaded_bodies
```

- [ ] **Step 3: Replace `_run` body**

Rewrite `_run`:

```python
async def _run(records: list[dict[str, Any]], config: Config) -> dict[str, int]:
    """Resolve offloaded MIL bodies, normalize, extract attachments, build + push events."""
    await resolve_offloaded_bodies(records)

    async def _prepare(record: dict[str, Any]) -> tuple[NormalizedInvocation, dict[str, Any]]:
        normalized = normalize_record(record)
        attachments = await extract_converse_attachments(
            record,
            include_raw_content=config.include_raw_content,
            max_content_size=config.max_content_size,
        )
        normalized.accessed_files.extend(attachments)
        finalize(
            normalized,
            include_raw_content=config.include_raw_content,
            max_content_size=config.max_content_size,
        )
        return normalized, record

    prepared = await asyncio.gather(*(_prepare(r) for r in records))

    built_or_none = await asyncio.gather(
        *(
            build_event(
                normalized,
                record,
                include_raw_content=config.include_raw_content,
                max_content_size=config.max_content_size,
            )
            for normalized, record in prepared
        )
    )
    events = [e for e in built_or_none if e is not None]
    for e in events:
        _log_event(e)

    timeout = httpx.Timeout(config.request_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        event_count = await push_invocations(
            client,
            events,
            endpoint=config.endpoint,
            push_token=config.push_token,
            max_retries=config.max_retries,
        )

    return {"events_pushed": event_count, "records_seen": len(records)}
```

### Task 4.5: Drop `aioboto3` from `shared/pyproject.toml`, add to `bedrock/pyproject.toml`

- [ ] **Step 1: `shared/pyproject.toml`**

Remove:

```toml
    # aioboto3 is here (rather than in bedrock/) because
    # slashid_ai_forwarder_core.events._accessed_files lazily imports
    # slashid_ai_forwarder_core.s3._resolve_s3_attachment. Making the
    # attachment resolver injectable (so shared/events.py is vendor-
    # neutral and non-AWS forwarders don't pull aioboto3) is tracked
    # as a Phase 2 cleanup.
    "aioboto3>=15.0",
```

Save the resulting deps block clean (just the remaining entries in order).

- [ ] **Step 2: `bedrock/pyproject.toml`**

Add to the `[project].dependencies` list:

```toml
    "aioboto3>=15.0",
```

Also add a brief comment above:

```toml
    # Owns S3 attachment resolution + MIL body offload — moved from shared/
    # in Phase 2.1.
    "aioboto3>=15.0",
```

- [ ] **Step 3: `uv sync --all-packages`**

Run: `uv sync --all-packages`
Expected: `aioboto3` migrates from shared to bedrock's dep tree; total install unchanged.

### Task 4.6: Migrate `shared/tests/test_events.py::test_accessed_files_*` tests

The 21 `test_accessed_files_*` tests split three ways:

1. **6 tool-result-focused** — remove entirely; already covered by `test_tool_results.py` (Chunk 2 YAML fixtures cover the corresponding scenarios). Test names to delete: `test_accessed_files_tool_result_read`, `test_accessed_files_tool_result_read_no_prefix_falls_back`, `test_accessed_files_tool_result_raw_content_opt_in`, `test_accessed_files_tool_result_readfile_variant`, `test_accessed_files_unknown_tool_ignored`, `test_accessed_files_tool_result_only_last_turn`.
2. **14 attachment-focused** — migrate to `bedrock/tests/test_converse_attachments.py` (Task 4.7).
3. **1 cross-path** (`test_accessed_files_dedup_tool_and_attachment`) — migrate to `bedrock/tests/test_handler.py` (Task 4.8).

- [ ] **Step 1: Delete the 6 tool-result-focused test functions**

Edit `shared/tests/test_events.py` and delete each of the 6 named functions above.

- [ ] **Step 2: Verify no ripple**

Run: `cd shared && uv run pytest tests/test_events.py -q`
Expected: previous count minus 6 tests, no failures. If failures happen because the 6 deleted tests referenced helper functions (`_record_with_tool_call`, etc.), verify those helpers are still used by other tests — if not, delete them too.

Run: `grep -n "_record_with_tool_call\|_record_with_bash" shared/tests/test_events.py`
If `_record_with_tool_call` has no callers left, delete it. `_record_with_bash` is used by other tests — keep.

### Task 4.7: Create `bedrock/tests/test_converse_attachments.py`

Take the 14 attachment-focused tests. Each currently:

```python
async def test_accessed_files_document_inline() -> None:
    ...
    event = await build_event(converse_dict_to_normalized(record), record)
    ...
    assert event.accessed_files == [...]
```

Rewrite each to call `extract_converse_attachments(record, ...)` directly (bedrock module), asserting on the returned list.

- [ ] **Step 1: Create the file with the migrated tests**

Create `bedrock/tests/test_converse_attachments.py`. For each of the 14 tests, transplant the record-building code and rewrite the assertion. Example rewrite of `test_accessed_files_document_inline`:

```python
"""Migrated from shared/tests/test_events.py — attachment-focused subset of
the former _accessed_files tests, now exercising the bedrock-side
extract_converse_attachments directly."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

import pytest

from slashid_bedrock_forwarder.converse_attachments import extract_converse_attachments


def _mil_record(**overrides: Any) -> dict[str, Any]:
    """Minimal MIL record scaffold — mirrors _mil_record in shared/tests/test_events.py
    but strips fields converse_attachments doesn't read (identity, model, tokens)."""
    base: dict[str, Any] = {
        "input": {"inputBodyJson": {}},
        "output": {"outputBodyJson": {}},
    }
    base.update(overrides)
    return base


def _record_with_messages(messages: list[Any]) -> dict[str, Any]:
    return _mil_record(input={"inputBodyJson": {"messages": messages}})


async def test_document_inline() -> None:
    content = b"hello world"
    b64 = base64.b64encode(content).decode()
    record = _record_with_messages([
        {"role": "user", "content": [
            {"document": {
                "name": "notes",
                "format": "txt",
                "source": {"bytes": b64},
            }},
            {"text": "summarize"},
        ]},
    ])
    files = await extract_converse_attachments(
        record, include_raw_content=False, max_content_size=100_000
    )
    assert len(files) == 1
    assert files[0].name == "notes"
    assert files[0].media_type == "text/plain"
    assert files[0].byte_length == len(content)
    assert files[0].content_hashes == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "sha1": hashlib.sha1(content).hexdigest(),
        "md5": hashlib.md5(content).hexdigest(),
    }


async def test_document_raw_content_opt_in() -> None:
    ...
```

Continue for all 14: `document_inline`, `document_raw_content_opt_in`, `image_inline`, `s3_source_uses_uri_as_name`, `s3uri_shape`, `s3_content_type_used_as_media_type_fallback`, `mime_map`, `media_type_from_filename_fallback`, `stub_has_media_type_from_filename`, `non_dict_input_body_returns_empty`, `deduplicates_within_same_window`, `only_from_last_user_turn`, `all_included_when_no_prior_assistant_turn`, `none_when_no_attachments`.

For S3-source tests that used `monkeypatch.setattr(s3, "fetch_offloaded_body", ...)`, the import path changes from `slashid_ai_forwarder_core.s3` to `slashid_bedrock_forwarder.s3`. Update accordingly.

- [ ] **Step 2: Run**

Run: `cd bedrock && uv run pytest tests/test_converse_attachments.py -v`
Expected: 14 tests pass.

### Task 4.8: Add end-to-end test to `bedrock/tests/test_handler.py`

- [ ] **Step 1: Add a single sanity test covering the composed pipeline + cross-path dedup**

Append to `bedrock/tests/test_handler.py`:

```python
async def test_run_populates_accessed_files_from_both_extractors() -> None:
    """End-to-end: _run composes bedrock attachment extractor + shared
    finalize; both paths' AIAccessedFile entries land on the wire event.
    Also verifies the cross-path (name, sha256) dedup — a file surfaced
    by both extractors appears once."""
    ...
```

Base this on the current `shared/tests/test_events.py::test_accessed_files_dedup_tool_and_attachment` fixture. Adapt to invoke `_run` via a fake `push_invocations`-style intercept or directly assert on the event stream — whichever pattern the existing `test_handler.py` follows for other end-to-end tests. Run to green.

### Task 4.9: Full-suite regression + commit

- [ ] **Step 1: Full suites + audit greps**

Run: `(cd shared && uv run pytest -q) && (cd bedrock && uv run pytest -q)`
Expected: all green.

Run: `grep -rn "aioboto3" shared/`
Expected: no matches.

Run: `grep -rn "from slashid_ai_forwarder_core.s3\|from \.s3 import" shared/ bedrock/`
Expected: matches only in `bedrock/src/slashid_bedrock_forwarder/*.py` and `bedrock/tests/`.

Run: `grep -rn "_accessed_files\|_READ_TOOLS\|_ToolSpec\|_extract_otel\|_otel_from" shared/src/`
Expected: no matches (except `extract_otel` in the leaf module `otel.py`).

- [ ] **Step 2: Ruff + ty + format**

Run: `uv run ty check && uv run ruff check shared/ bedrock/ && (cd shared && uv run ruff format --check .) && (cd bedrock && uv run ruff format --check .)`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add -u
git add bedrock/src/slashid_bedrock_forwarder/s3.py \
        bedrock/src/slashid_bedrock_forwarder/converse_attachments.py \
        bedrock/tests/test_s3.py \
        bedrock/tests/test_converse_attachments.py
git commit -m "$(cat <<'EOF'
refactor: split boto out of shared/, extract accessed_files canonically

Atomic switch:
- shared/s3.py → bedrock/s3.py (verbatim). shared drops the aioboto3 dep.
- New bedrock/converse_attachments.py owns Bedrock document/image block
  extraction + S3-source resolution. Vendor-specific attachment handling
  now lives in the Bedrock subproject.
- shared/events.py::_accessed_files deleted (~300 lines gone). build_event
  reads normalized.accessed_files directly.
- bedrock/handler.py::_run composes normalize_record + extract_converse_
  attachments + finalize before build_event. Tool-result → accessed_files
  now vendor-agnostic (fires uniformly for Anthropic + Converse via the
  canonical) and preserves the is_error skip from the retired PR #16.

Wire behaviour unchanged: same AIInvocationObservedV1.accessed_files
entries, same hashes, same byte_lengths — verified by round-tripping the
migrated tests. Only intentional delta: is_error tool_results no longer
produce a misleading accessed_files entry hashed against the error body.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Chunk 5: Release

Version bump + smoke-test README refresh + PR.

**Files:**

- Modify: `bedrock/pyproject.toml` — version bump
- Modify: `bedrock/README.md` — smoke-test section note

### Task 5.1: Version bump

- [ ] **Step 1: Grep for the version string**

Run: `grep -n '"0.1.2"' bedrock/pyproject.toml`
Expected: one match, on the `version =` line.

- [ ] **Step 2: Edit**

Replace `version = "0.1.2"` → `version = "0.1.3"` in `bedrock/pyproject.toml`.

- [ ] **Step 3: `uv sync`**

Run: `uv sync --all-packages`
Expected: bedrock package rebuild.

### Task 5.2: README note

- [ ] **Step 1: Add a brief note under the Read-tool smoke recipe**

In `bedrock/README.md`, extend the "Via Claude Code exercising the `Read` tool" section:

```markdown
Post-v0.1.3, if the Read tool errors (e.g. permission-refused), the
tool result no longer produces an `accessed_files` entry — the
`used_tools[i].is_error: true` signal is preserved, but hashing the
error-message body under the requested file path (misleading) is
skipped.
```

### Task 5.3: Full-suite final check + commit

- [ ] **Step 1: Full-suite regression**

Run: `(cd shared && uv run pytest -q) && (cd bedrock && uv run pytest -q) && uv run ty check && uv run ruff check shared/ bedrock/ && (cd shared && uv run ruff format --check .) && (cd bedrock && uv run ruff format --check .)`
Expected: all green.

- [ ] **Step 2: Commit**

```bash
git add bedrock/pyproject.toml bedrock/README.md uv.lock
git commit -m "$(cat <<'EOF'
chore(release): bump bedrock 0.1.2 → 0.1.3

Phase 2.1: accessed_files canonicalization + boto split. Wire behaviour
unchanged; smoke recipes unchanged. README notes the is_error skip
(no more misleading error-body hashes under the file path).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

### Task 5.4: Push + PR (STOP HERE)

- [ ] **Step 1: Push**

Run: `git push -u origin accessed-files-canonicalization`

- [ ] **Step 2: Open PR**

Run:

```bash
gh pr create --title "refactor: canonicalize accessed_files + split boto out of shared/" --body "$(cat <<'EOF'
## Summary

- `NormalizedInvocation` gains `accessed_files: list[AIAccessedFile] = Field(default_factory=list)` (drive-by: all list-typed fields on `NormalizedInvocation` / `NormalizedInvocationInput` flip to non-null default `[]`).
- Bedrock-specific attachment handling (`{document}` / `{image}` blocks, S3 source resolution) moves out of `shared/events.py::_accessed_files` into `bedrock/src/slashid_bedrock_forwarder/converse_attachments.py`. `shared/s3.py` moves to `bedrock/s3.py`; `shared/` drops the `aioboto3` dep.
- Vendor-agnostic tool-result extraction (Read-tool → AIAccessedFile) moves into `shared/normalize/normalized/tool_results.py`, walks the canonical `NormalizedInvocation.input.messages`, works uniformly for Anthropic + Converse.
- OTel context extraction (`extract_otel`) moves to a leaf module (`shared/normalize/normalized/otel.py`) so both `events.py::_used_tools` and `tool_results.py::extract_tool_result_files` can import it without a cycle.
- `shared/normalize/finalize.py::finalize` is the single post-hook that composes the pieces: bedrock handler calls `normalize_record` → `extract_converse_attachments` → `finalize` → `build_event`.
- `shared/events.py::_accessed_files` deleted (~300 lines).

**Wire behaviour unchanged.** Same `AIInvocationObservedV1.accessed_files` shape, same hashes, same `byte_length`. Only intentional delta: `Read` tool errors no longer produce a misleading `accessed_files` entry hashed against the error-message body — the `used_tools[i].is_error: true` signal is preserved.

Supersedes closed [PR #16](https://github.com/slashid/slashid-ai-forwarders/pull/16) — the `is_error` guard rolls into the new tool-result extractor.

Design doc: `docs/superpowers/specs/2026-09-04-accessed-files-canonicalization.md`.

## Test plan

- [x] `cd shared && uv run pytest -q` — green (new: `test_otel.py`, `test_tool_results.py`, `test_finalize.py`, extended `test_types_round_trip.py`; removed: 6 tool-result-focused `test_accessed_files_*` tests).
- [x] `cd bedrock && uv run pytest -q` — green (new: `test_s3.py` moved from shared, `test_converse_attachments.py` migrated from shared, one end-to-end test in `test_handler.py`).
- [x] `uv run ty check` — clean.
- [x] `uv run ruff check shared/ bedrock/` + `ruff format --check` — clean.
- [x] `grep -rn "aioboto3" shared/` returns nothing.
- [x] `grep -rn "_accessed_files\|_READ_TOOLS\|_extract_otel\|_otel_from" shared/src/` returns nothing (all moved to `normalize/normalized/{otel,tool_results}.py`).
- [ ] Round-trip diff against a captured live MIL record (Nova + Claude, with attachments): `AIInvocationObservedV1.accessed_files` identical entry-for-entry, hash-for-hash, to pre-refactor emission.
- [ ] Deploy verification (post-merge, manual): `bedrock-v0.1.3` released → deployed → re-run `./converse-attach ./notes.txt`, `./converse-attach ./chart.png`, `./claude "please Read SMOKE_TARGET.txt ..."` — event JSON matches pre-refactor accessed_files entries.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: STOP.** Per the user's durable rule (`no_auto_merge_prs.md`), do NOT merge. Report PR URL and wait.

---

## Verification (cross-chunk)

Run at the end of Chunk 4 and again at the end of Chunk 5:

1. `(cd shared && uv run pytest -q) && (cd bedrock && uv run pytest -q)` — all green.
2. `uv run ty check && uv run ruff check shared/ bedrock/` — clean.
3. `(cd shared && uv run ruff format --check .) && (cd bedrock && uv run ruff format --check .)` — clean.
4. `grep -rn "aioboto3" shared/` — no matches.
5. `grep -rn "_accessed_files\|_READ_TOOLS\|_extract_otel" shared/src/` — no matches (all names now live only in the new modules or as `extract_otel` in `otel.py`).
6. `grep -rn "from .s3 import\|from slashid_ai_forwarder_core.s3" shared/ bedrock/` — matches only in `bedrock/`.

## Rollback

Standard: `cloudformation update-stack` back to `bedrock-v0.1.2`. No wire schema change. Downstream consumers see identical event shape. The one behaviour delta (is_error skip) is a correctness fix; consumers that relied on the misleading behaviour would already be broken.
