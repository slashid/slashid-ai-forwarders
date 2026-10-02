from __future__ import annotations

from slashid_ai_forwarder_core.events import AIInvocationTokens

from slashid_codex.usage import CodexUsage, codex_usage_to_tokens


def test_codex_usage_is_additive() -> None:
    usage = CodexUsage(
        input_tokens=15189,
        cached_input_tokens=0,
        cache_write_input_tokens=15186,
        output_tokens=43,
        reasoning_output_tokens=0,
    )
    assert codex_usage_to_tokens(usage) == AIInvocationTokens(
        input=3, cache_read=0, cache_write=15186, output=43, reasoning=0
    )


def test_reasoning_and_cache_read_split_out() -> None:
    usage = CodexUsage.model_validate_json(
        '{"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 30,'
        ' "reasoning_output_tokens": 12, "total_tokens": 130}'
    )
    assert codex_usage_to_tokens(usage) == AIInvocationTokens(
        input=40, cache_read=60, cache_write=0, output=18, reasoning=12
    )
