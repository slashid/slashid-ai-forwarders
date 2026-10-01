"""Vendor-agnostic post-processing pass — canonicalizes accessed_files.

Composes on top of any vendor-side attachment extractor (e.g. the
Converse document/image walker inside
``normalize.converse.normalize.to_normalized_invocation``): the caller
runs the vendor translate first (writes to ``normalized.accessed_files``),
then calls ``finalize`` to combine vendor entries with tool-result-
derived entries and dedupe the union in one pass.

Dedup uses a multi-algorithm key: two entries collide if they share
``(name, alg, hash_value)`` for ANY algorithm they both hash under.
GCS metadata only exposes md5 for objects uploaded outside a
compose/rewrite path; S3 HEAD gives ETag (often md5). Sha256 is what
inline decoders compute. Keying on ``sha256`` alone would leak
cross-source duplicates whenever one side is md5-only. First-seen
wins — vendor entries come first, so an inline attachment beats a
Read tool_result of the same file.
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

    Combines vendor-extracted files with tool-result-derived files and
    dedupes the union in a single pass (see ``dedupe_by_name_hash`` for
    the multi-algorithm key rule). Vendor entries come first so they
    win ties.
    """
    tool_files = extract_tool_result_files(normalized.input.messages, config=config)
    normalized.accessed_files = dedupe_by_name_hash(normalized.accessed_files + tool_files)
    return normalized


def _hash_triples(f: AIAccessedFile) -> set[tuple[str | None, str | None, str | None]]:
    """Set of ``(name, alg, value)`` triples identifying this file.

    Stub entries (no ``content_hashes``) return a single ``(name, None,
    None)`` sentinel — stubs with the same name collide with each
    other, but not with fully-hashed entries. That preserves the
    "referenced but unfetched" signal for both S3 HEAD failures and
    ``fileData`` ``gs://`` stubs.
    """
    if not f.content_hashes:
        return {(f.name, None, None)}
    return {(f.name, alg, value) for alg, value in f.content_hashes.items()}


def dedupe_by_name_hash(files: list[AIAccessedFile]) -> list[AIAccessedFile]:
    """First-seen-wins dedup. Two entries collide iff they share ANY
    ``(name, alg, value)`` triple — an md5-only entry from one
    extractor matches a sha256+md5 entry from another via the shared
    md5 hash.

    ``seen`` accumulates triples from EVERY entry, even the ones we
    skip — a skipped middle entry that carries both md5 (matching a
    prior md5-only) and sha256 leaves its sha256 in ``seen`` so a
    later sha256-only entry for the same bytes also collapses. Without
    this propagation the md5→sha256 bridge would fall out and the
    later entry would leak through as a duplicate.
    """
    seen: set[tuple[str | None, str | None, str | None]] = set()
    out: list[AIAccessedFile] = []
    for f in files:
        triples = _hash_triples(f)
        collides = bool(triples & seen)
        seen |= triples
        if not collides:
            out.append(f)
    return out
