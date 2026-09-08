"""Gemini finishReason → AIStopReason mapping tests."""

from __future__ import annotations

from slashid_ai_forwarder_core.events import AIStopReason
from slashid_ai_forwarder_core.normalize.gemini.stop_reasons import STOP_REASONS
from slashid_ai_forwarder_core.testing import yaml_pytest


@yaml_pytest()
def test_gemini_stop_reasons(raw: str | None, expected: AIStopReason) -> None:
    assert STOP_REASONS.get(raw or "", "unknown") == expected


# --------------------------------------------------------------------------
# resolve_finish_reason — streaming stop_reason recovery helper
# --------------------------------------------------------------------------

from slashid_ai_forwarder_core.normalize.gemini.stop_reasons import (
    resolve_finish_reason,
)


def test_resolve_finish_reason_passes_through_explicit() -> None:
    """A concrete finishReason maps through STOP_REASONS unchanged —
    max_output_tokens is irrelevant when the API already told us."""
    assert (
        resolve_finish_reason("STOP", candidates_token_count=5, max_output_tokens=100)
        == "end_turn"
    )
    assert (
        resolve_finish_reason(
            "MAX_TOKENS", candidates_token_count=100, max_output_tokens=100
        )
        == "max_tokens"
    )
    assert (
        resolve_finish_reason("SAFETY", candidates_token_count=5, max_output_tokens=None)
        == "content_filtered"
    )


def test_resolve_finish_reason_null_no_cap_defaults_to_stop() -> None:
    """Streamed response with no explicit cap → default to STOP.
    A merged log entry exists only when the stream completed, so
    end_turn is safe."""
    assert (
        resolve_finish_reason(None, candidates_token_count=42, max_output_tokens=None)
        == "end_turn"
    )


def test_resolve_finish_reason_null_under_cap_defaults_to_stop() -> None:
    """Cap set but candidate tokens under the cap → still end_turn.
    Model finished before hitting the limit."""
    assert (
        resolve_finish_reason(None, candidates_token_count=42, max_output_tokens=1000)
        == "end_turn"
    )


def test_resolve_finish_reason_null_at_cap_recovers_max_tokens() -> None:
    """Cap set and candidate tokens equal to the cap → MAX_TOKENS
    recovered. Verified empirically in the POC (2026-09-08)."""
    assert (
        resolve_finish_reason(None, candidates_token_count=100, max_output_tokens=100)
        == "max_tokens"
    )


def test_resolve_finish_reason_null_over_cap_still_max_tokens() -> None:
    """Vertex may report candidate tokens slightly over the cap due
    to tokenizer rounding — still infer MAX_TOKENS."""
    assert (
        resolve_finish_reason(None, candidates_token_count=101, max_output_tokens=100)
        == "max_tokens"
    )


def test_resolve_finish_reason_unknown_finish_falls_through() -> None:
    """Unknown non-null finishReason values still fall to "unknown"
    via STOP_REASONS.get default. Preserves the existing safety net."""
    assert (
        resolve_finish_reason(
            "SOME_NEW_ENUM", candidates_token_count=5, max_output_tokens=None
        )
        == "unknown"
    )
