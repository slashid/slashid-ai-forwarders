"""Tests for finalize — the single-pass tool-result post-hook."""

from __future__ import annotations

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


def test_finalize_is_idempotent_via_cross_dedup() -> None:
    """A second call skips the tool-result files whose (name, sha256) already
    appears in ``normalized.accessed_files`` — the cross-path dedup ensures
    idempotency for identical inputs. Preserves pre-refactor wire behaviour
    where the shared _accessed_files walk deduped attachments and
    tool-results in a single ``seen`` set."""
    n = _invocation_with_read("/tmp/x.txt", "hello")
    finalize(n, include_raw_content=False, max_content_size=100_000)
    finalize(n, include_raw_content=False, max_content_size=100_000)
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
    finalize(n, include_raw_content=False, max_content_size=100_000)
    assert len(n.accessed_files) == 1
    assert n.accessed_files[0].byte_length == 5


def test_finalize_returns_same_instance() -> None:
    """Mutation, not clone — the returned object is the same instance."""
    n = NormalizedInvocation()
    result = finalize(n, include_raw_content=False, max_content_size=100_000)
    assert result is n


def test_finalize_include_raw_content_populates_redacted() -> None:
    n = _invocation_with_read("/tmp/x.txt", "secret content")
    finalize(n, include_raw_content=True, max_content_size=100_000)
    assert n.accessed_files[0].redacted_content == "secret content"
