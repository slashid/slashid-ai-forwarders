"""OpenAI usage → additive ``AIInvocationTokens``.

OpenAI's ``input_tokens`` includes cached and cache-write tokens, and
``output_tokens`` includes reasoning tokens; the wire model wants them
disjoint.
"""

from __future__ import annotations

from ...events import AIInvocationTokens
from .chat.schema import ChatUsage
from .responses.schema import ResponsesUsage


def additive_tokens(
    *, input_total: int, cached: int, cache_write: int, output_total: int, reasoning: int
) -> AIInvocationTokens:
    return AIInvocationTokens(
        input=max(input_total - cached - cache_write, 0),
        cache_read=cached,
        cache_write=cache_write,
        output=max(output_total - reasoning, 0),
        reasoning=reasoning,
    )


def responses_usage_to_tokens(usage: ResponsesUsage | None) -> AIInvocationTokens:
    if usage is None:
        return AIInvocationTokens()
    input_details = usage.input_tokens_details
    output_details = usage.output_tokens_details
    return additive_tokens(
        input_total=usage.input_tokens,
        cached=input_details.cached_tokens if input_details else 0,
        cache_write=input_details.cache_write_tokens if input_details else 0,
        output_total=usage.output_tokens,
        reasoning=output_details.reasoning_tokens if output_details else 0,
    )


def chat_usage_to_tokens(usage: ChatUsage | None) -> AIInvocationTokens:
    if usage is None:
        return AIInvocationTokens()
    prompt_details = usage.prompt_tokens_details
    completion_details = usage.completion_tokens_details
    return additive_tokens(
        input_total=usage.prompt_tokens,
        cached=prompt_details.cached_tokens if prompt_details else 0,
        cache_write=prompt_details.cache_write_tokens if prompt_details else 0,
        output_total=usage.completion_tokens,
        reasoning=completion_details.reasoning_tokens if completion_details else 0,
    )
