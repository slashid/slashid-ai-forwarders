"""Tests for finalize — the single-pass tool-result post-hook."""

from __future__ import annotations

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedMessage,
)


def _config(*, include_raw_content: bool = False, max_content_size: int = 100_000) -> BaseConfig:
    return BaseConfig(
        endpoint="http://test",
        push_token="test",
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )


def _invocation_with_read(path: str, tool_output: str, *, is_error: bool = False):
    """Build a minimal invocation with one Read/tool_result pair."""
    return NormalizedInvocation(
        input=NormalizedInvocationInput(
            messages=[
                NormalizedMessage(
                    role="assistant",
                    content=[
                        NormalizedContent(
                            kind="tool_use",
                            tool_use_id="tu_1",
                            tool_name="Read",
                            tool_input={"file_path": path},
                        )
                    ],
                ),
                NormalizedMessage(
                    role="user",
                    content=[
                        NormalizedContent(
                            kind="tool_result",
                            tool_use_id="tu_1",
                            tool_output=tool_output,
                            tool_is_error=is_error,
                        )
                    ],
                ),
            ],
        ),
    )


def test_finalize_empty_invocation() -> None:
    """No messages → no tool-result files. accessed_files stays empty."""
    n = NormalizedInvocation()
    finalize(n, config=_config())
    assert n.accessed_files == []


def test_finalize_appends_tool_result_files() -> None:
    n = _invocation_with_read("/tmp/x.txt", "hello world")
    finalize(n, config=_config())
    assert len(n.accessed_files) == 1
    assert n.accessed_files[0].name == "/tmp/x.txt"
    assert n.accessed_files[0].byte_length == 11


def test_finalize_skips_is_error() -> None:
    """is_error=True → no accessed_files entry."""
    n = _invocation_with_read("/tmp/x.txt", "permission denied", is_error=True)
    finalize(n, config=_config())
    assert n.accessed_files == []


def test_finalize_preserves_pre_existing_entries() -> None:
    """If accessed_files was already populated (e.g. by bedrock-side attachment
    extractor), finalize appends to it, doesn't replace."""
    n = _invocation_with_read("/tmp/x.txt", "hello")
    n.accessed_files.append(AIAccessedFile(name="/attach/pdf", byte_length=1024))
    finalize(n, config=_config())
    names = [f.name for f in n.accessed_files]
    assert names == ["/attach/pdf", "/tmp/x.txt"]


def test_finalize_is_idempotent_via_cross_dedup() -> None:
    """A second call skips the tool-result files whose (name, sha256) already
    appears in ``normalized.accessed_files`` — the cross-path dedup ensures
    idempotency for identical inputs. Preserves pre-refactor wire behaviour
    where the shared _accessed_files walk deduped attachments and
    tool-results in a single ``seen`` set."""
    n = _invocation_with_read("/tmp/x.txt", "hello")
    finalize(n, config=_config())
    finalize(n, config=_config())
    assert len(n.accessed_files) == 1


def test_finalize_cross_dedup_against_attachment() -> None:
    """A tool-result whose (name, sha256) matches a pre-existing attachment
    entry is skipped — the cross-path dedup between vendor attachment
    extractors and the tool-result extractor."""
    import hashlib as _h

    n = _invocation_with_read("/tmp/x.txt", "hello")
    n.accessed_files.append(
        AIAccessedFile(
            name="/tmp/x.txt",
            content_hashes={"sha256": _h.sha256(b"hello").hexdigest()},
            byte_length=5,
        )
    )
    finalize(n, config=_config())
    assert len(n.accessed_files) == 1
    assert n.accessed_files[0].byte_length == 5


def test_finalize_returns_same_instance() -> None:
    """Mutation, not clone — the returned object is the same instance."""
    n = NormalizedInvocation()
    result = finalize(n, config=_config())
    assert result is n


def test_finalize_include_raw_content_populates_redacted() -> None:
    n = _invocation_with_read("/tmp/x.txt", "secret content")
    finalize(n, config=_config(include_raw_content=True))
    assert n.accessed_files[0].redacted_content == "secret content"


def test_finalize_dedupes_across_hash_algorithms() -> None:
    """Two entries for the same file that share ``md5`` but differ in
    the sha256/sha1 availability still dedupe — matches the scenario
    where a GCS metadata source contributes an md5-only entry that a
    sha256+md5 inline extractor would otherwise duplicate."""
    import hashlib as _h

    content = b"hello world"
    n = NormalizedInvocation()
    # Vendor extractor: all three hashes.
    n.accessed_files.append(
        AIAccessedFile(
            name="report.pdf",
            content_hashes={
                "sha256": _h.sha256(content).hexdigest(),
                "sha1": _h.sha1(content).hexdigest(),
                "md5": _h.md5(content).hexdigest(),
            },
            byte_length=len(content),
        )
    )
    # Same file surfaced later with only md5 — collides via shared md5.
    n.accessed_files.append(
        AIAccessedFile(
            name="report.pdf",
            content_hashes={"md5": _h.md5(content).hexdigest()},
        )
    )
    finalize(n, config=_config())
    assert len(n.accessed_files) == 1
    # First-seen wins — the fully-hashed vendor entry is kept.
    assert n.accessed_files[0].content_hashes is not None
    assert set(n.accessed_files[0].content_hashes) == {"sha256", "sha1", "md5"}


def test_finalize_does_not_collide_different_files_with_different_algs() -> None:
    """Two different files, one sha256-only and one md5-only, must NOT
    collide — the (name, alg, value) triples don't intersect."""
    n = NormalizedInvocation()
    n.accessed_files.append(
        AIAccessedFile(name="a.txt", content_hashes={"sha256": "aaa"}),
    )
    n.accessed_files.append(
        AIAccessedFile(name="b.txt", content_hashes={"md5": "bbb"}),
    )
    finalize(n, config=_config())
    assert len(n.accessed_files) == 2


def test_finalize_hash_knowledge_propagates_through_skipped_entries() -> None:
    """A middle entry that collides on md5 but ALSO carries a sha256
    leaves that sha256 in the seen-set, so a later sha256-only entry
    for the same bytes still collapses.

    Sequence:
      A: {md5: X}       — kept, seen = {md5:X}
      B: {md5: X, sha256: Y} — collides via md5, skipped, but its
                              sha256:Y is added to seen anyway.
      C: {sha256: Y}    — would leak through with only A's triples in
                          seen; collapses now that B's sha256 bridged.
    """
    n = NormalizedInvocation()
    n.accessed_files.append(AIAccessedFile(name="doc", content_hashes={"md5": "X"}))
    n.accessed_files.append(AIAccessedFile(name="doc", content_hashes={"md5": "X", "sha256": "Y"}))
    n.accessed_files.append(AIAccessedFile(name="doc", content_hashes={"sha256": "Y"}))
    finalize(n, config=_config())
    assert len(n.accessed_files) == 1


def test_finalize_stubs_dedupe_by_name_but_not_against_hashed() -> None:
    """Stub entries (no content_hashes) collide with other same-name
    stubs, but stay distinct from a hashed entry with the same name —
    the hashed entry contributes real (name, alg, value) triples while
    the stub carries only the (name, None, None) sentinel."""
    n = NormalizedInvocation()
    n.accessed_files.append(AIAccessedFile(name="gs://bucket/x", content_hashes=None))
    n.accessed_files.append(AIAccessedFile(name="gs://bucket/x", content_hashes=None))
    n.accessed_files.append(AIAccessedFile(name="gs://bucket/x", content_hashes={"sha256": "abc"}))
    finalize(n, config=_config())
    # 3 → 2: stubs collapse, hashed entry stays.
    assert len(n.accessed_files) == 2
