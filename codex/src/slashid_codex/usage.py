"""Codex ``token_usage_record`` usage → additive ``AIInvocationTokens``."""

from __future__ import annotations

from slashid_ai_forwarder_core.events import AIInvocationTokens
from slashid_ai_forwarder_core.normalize._base import _LenientModel
from slashid_ai_forwarder_core.normalize.openai.usage import additive_tokens


class CodexUsage(_LenientModel):
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0


def codex_usage_to_tokens(usage: CodexUsage) -> AIInvocationTokens:
    return additive_tokens(
        input_total=usage.input_tokens,
        cached=usage.cached_input_tokens,
        cache_write=usage.cache_write_input_tokens,
        output_total=usage.output_tokens,
        reasoning=usage.reasoning_output_tokens,
    )
