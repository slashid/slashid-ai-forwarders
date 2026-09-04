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
