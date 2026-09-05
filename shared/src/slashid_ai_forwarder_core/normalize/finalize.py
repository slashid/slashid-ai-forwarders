"""Vendor-agnostic post-processing pass — populates canonical accessed_files
with tool-result-derived entries.

Composes on top of any vendor-side attachment extractor (e.g. the
Converse document/image walker inside
``normalize.converse.normalize.to_normalized_invocation``): the caller
runs the vendor translate first (writes to ``normalized.accessed_files``),
then calls ``finalize`` to append tool-result-derived entries in a single
pass.

Cross-path dedup: tool-result files whose (name, sha256) matches an
already-present entry (e.g. a Bedrock document attachment) are skipped.
Preserves the pre-refactor behaviour where the shared _accessed_files
walk deduped attachments and tool-results in a single ``seen`` set.
"""

from __future__ import annotations

from ..config_base import BaseConfig
from .normalized.tool_results import extract_tool_result_files
from .normalized.types import NormalizedInvocation


def finalize(
    normalized: NormalizedInvocation,
    *,
    config: BaseConfig,
) -> NormalizedInvocation:
    """Append tool-result files to ``normalized.accessed_files``. Returns the
    same instance (mutation, not clone) for chainable use.

    Skips entries whose (name, sha256) already appears in
    ``normalized.accessed_files`` — cross-path dedup between vendor
    attachment extractors and the tool-result extractor.
    """
    tool_files = extract_tool_result_files(
        normalized.input.messages,
        config=config,
    )
    seen: set[tuple[str | None, str | None]] = {
        (f.name, f.content_hashes.get("sha256") if f.content_hashes else None)
        for f in normalized.accessed_files
    }
    for f in tool_files:
        key = (f.name, f.content_hashes.get("sha256") if f.content_hashes else None)
        if key in seen:
            continue
        seen.add(key)
        normalized.accessed_files.append(f)
    return normalized
