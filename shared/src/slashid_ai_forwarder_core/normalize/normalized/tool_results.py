"""Vendor-agnostic Read-tool → AIAccessedFile extraction over canonical messages.

Walks ``NormalizedInvocation.input.messages`` for tool_use/tool_result
pairs matching the ``_READ_TOOLS`` table (Claude Code Read, OpenCode /
Amazon Q / Gemini CLI ReadFile / read_file / view_file, Claude
computer-use text-editor tool, single-file shell reads, Codex view_image).
For each match: hashes the returned bytes (with per-tool cleanup — e.g.
``strip_cat_n`` for Claude Code's line-number prefix), builds an
AIAccessedFile keyed by the tool's ``file_path`` / ``path`` argument.

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

from pydantic import BaseModel, ConfigDict, JsonValue

from ...config_base import BaseConfig
from ...content_utils import strip_cat_n, truncate_middle
from ...events import AIAccessedFile
from ..turn import after_last_assistant
from .codex_output import image_output_bytes, shell_read_path, shell_stdout, view_image_path
from .types import NormalizedMessage


class _ToolSpec(BaseModel):
    """Per-tool declaration: ``path_from`` derives the file path from the
    tool input, an optional cleanup function to apply to the returned content
    before hashing (or ``content_from`` to derive the bytes), and whether the
    content is ``binary`` and so never kept as text."""

    model_config = ConfigDict(frozen=True)
    path_from: Callable[[JsonValue], str | None]
    content_from: Callable[[JsonValue], bytes | None] | None = None
    cleanup: Callable[[str], str] | None = None
    binary: bool = False


def _field(name: str) -> Callable[[JsonValue], str | None]:
    def path_from(tool_input: JsonValue) -> str | None:
        if not isinstance(tool_input, dict):
            return None
        val = tool_input.get(name)
        return val if isinstance(val, str) and val else None

    return path_from


# Canonical reference: https://docs.anthropic.com/en/docs/claude-code/tools
_READ_TOOLS: dict[str, _ToolSpec] = {
    "Read": _ToolSpec(path_from=_field("file_path"), cleanup=strip_cat_n),  # Claude Code
    "ReadFile": _ToolSpec(path_from=_field("path")),  # OpenCode, Amazon Q Developer, Gemini CLI
    "read_file": _ToolSpec(path_from=_field("path")),  # snake_case variants
    "view_file": _ToolSpec(path_from=_field("path")),  # some agents
    "str_replace_based_edit_tool": _ToolSpec(
        path_from=_field("path")
    ),  # Claude computer-use text editor view
    "Bash": _ToolSpec(
        path_from=shell_read_path,
        content_from=lambda output: _bytes_from_tool_output(shell_stdout(output), None),
    ),  # Claude Code, Codex
    "view_image": _ToolSpec(
        path_from=view_image_path, content_from=image_output_bytes, binary=True
    ),  # Codex
}


def extract_tool_result_files(
    messages: list[NormalizedMessage],
    *,
    config: BaseConfig,
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

    # 2. For each fresh tool_result, correlate + hash.
    # Dedup key is (name, sha256) — matches Phase 1 behaviour. Edge case:
    # if content bytes couldn't be derived (empty tool_output, list of
    # non-text blocks), sha256 falls to None, and multiple different
    # tool_results for the same path collapse to one entry. Rare;
    # matches existing behaviour; live-safe.
    out: list[AIAccessedFile] = []
    seen: set[tuple[str, str | None]] = set()
    for msg in after_last_assistant(messages):
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

            path = spec.path_from(tool_input)
            if not path:
                continue

            if spec.content_from is not None:
                content_bytes = spec.content_from(block.tool_output)
            else:
                content_bytes = _bytes_from_tool_output(block.tool_output, spec.cleanup)
            file = _build_accessed_file(
                name=path,
                media_type=_mime_from_name(path),
                content_bytes=content_bytes,
                include_raw_content=config.include_raw_content,
                max_content_size=config.max_content_size,
                text_content=not spec.binary,
            )
            key = (path, file.content_hashes.get("sha256") if file.content_hashes else None)
            if key in seen:
                continue
            seen.add(key)
            out.append(file)
    return out


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
    text_content: bool = True,
) -> AIAccessedFile:
    """Assemble an AIAccessedFile with hashes / byte_length / optional redacted_content."""
    if content_bytes is not None:
        content_hashes: dict[str, str] | None = {
            "sha256": hashlib.sha256(content_bytes).hexdigest(),
            "sha1": hashlib.sha1(content_bytes).hexdigest(),
            "md5": hashlib.md5(content_bytes).hexdigest(),
        }
        byte_length: int | None = len(content_bytes)
    else:
        content_hashes = None
        byte_length = None
    redacted = None
    if include_raw_content and text_content and content_bytes is not None:
        redacted = truncate_middle(content_bytes.decode(errors="replace"), max_content_size)
    return AIAccessedFile(
        name=name,
        content_hashes=content_hashes,
        media_type=media_type,
        byte_length=byte_length,
        redacted_content=redacted,
        provenance="tool_result",
    )
