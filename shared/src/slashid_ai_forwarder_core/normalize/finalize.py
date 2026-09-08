"""Vendor-agnostic post-processing pass — canonicalizes accessed_files.

Composes on top of any vendor-side attachment extractor (e.g. the
Converse document/image walker inside
``normalize.converse.normalize.to_normalized_invocation``): the caller
runs the vendor translate first (writes to ``normalized.accessed_files``),
then calls ``finalize`` to (1) dedupe the vendor-extracted list and
(2) append tool-result-derived entries in a single pass.

Cross-path dedup: tool-result files whose (name, sha256) matches an
already-present entry (e.g. a Bedrock document attachment surfaced as
both an inline attachment and a Read tool_result) are skipped.
"""

from __future__ import annotations

from ..config_base import BaseConfig
from ..events import AIAccessedFile
from .normalized.tool_results import extract_tool_result_files
from .normalized.types import NormalizedInvocation


def finalize(
    normalized: NormalizedInvocation,
    *,
    config: BaseConfig,
) -> NormalizedInvocation:
    """Canonicalize ``normalized.accessed_files``. Returns the same
    instance (mutation, not clone) for chainable use.

    Two-step: dedupe the vendor-extracted list first (vendor extractors
    are allowed to emit duplicates — one input block per entry, no
    cross-block state), then append tool-result-derived files (skipping
    any whose (name, sha256) already appears).
    """
    normalized.accessed_files = _dedupe_by_name_sha256(normalized.accessed_files)

    tool_files = extract_tool_result_files(
        normalized.input.messages,
        config=config,
    )
    seen: set[tuple[str | None, str | None]] = {
        _dedupe_key(f) for f in normalized.accessed_files
    }
    for f in tool_files:
        key = _dedupe_key(f)
        if key in seen:
            continue
        seen.add(key)
        normalized.accessed_files.append(f)
    return normalized


def _dedupe_key(f: AIAccessedFile) -> tuple[str | None, str | None]:
    """Dedup key = ``(name, sha256)``. sha256 is None for stub entries
    (S3 HEAD-failed, fileData ``gs://`` stub, corrupt base64) — those
    fall back to name-only dedup within the same name."""
    sha = f.content_hashes.get("sha256") if f.content_hashes else None
    return f.name, sha


def _dedupe_by_name_sha256(files: list[AIAccessedFile]) -> list[AIAccessedFile]:
    """First-seen-wins dedup on ``(name, sha256)``."""
    seen: set[tuple[str | None, str | None]] = set()
    out: list[AIAccessedFile] = []
    for f in files:
        key = _dedupe_key(f)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out
