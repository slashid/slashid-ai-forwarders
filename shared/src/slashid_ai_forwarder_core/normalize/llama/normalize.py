"""Llama native ``InvokeModel`` → NormalizedInvocation.

The prompt is the model's own chat template as one string, kept whole as a
single user message; the answer is the ``generation`` text.
"""

from __future__ import annotations

from ...config_base import BaseConfig
from ...events import AIInvocationTokens, AIStopReason
from ..normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)
from .schema import LlamaRequest, LlamaResponse, LlamaStream, accumulate_stream


def to_normalized(request: LlamaRequest, response: LlamaResponse | None) -> NormalizedInvocation:
    prompt = [NormalizedContent(kind="text", text=request.prompt)]
    normalized = NormalizedInvocation(
        input=NormalizedInvocationInput(messages=[NormalizedMessage(role="user", content=prompt)])
    )
    if response is None:
        return normalized
    answer = (
        [NormalizedContent(kind="text", text=response.generation)] if response.generation else []
    )
    normalized.output = NormalizedInvocationOutput(
        message=NormalizedMessage(role="assistant", content=answer) if answer else None,
        stop_reason=_stop_reason(response.stop_reason),
    )
    normalized.tokens = AIInvocationTokens(
        input=response.prompt_token_count or 0, output=response.generation_token_count or 0
    )
    return normalized


async def llama_to_normalized_invocation(
    request: LlamaRequest, response: LlamaResponse, *, config: BaseConfig
) -> NormalizedInvocation:
    del config
    return to_normalized(request, response)


async def llama_stream_to_normalized_invocation(
    request: LlamaRequest, response: LlamaStream, *, config: BaseConfig
) -> NormalizedInvocation:
    del config
    return to_normalized(request, accumulate_stream(response))


def _stop_reason(stop_reason: str | None) -> AIStopReason:
    match stop_reason:
        case "stop":
            return "end_turn"
        case "length":
            return "max_tokens"
        case _:
            return "unknown"
