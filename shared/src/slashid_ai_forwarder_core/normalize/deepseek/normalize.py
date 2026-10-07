"""DeepSeek R1 native ``InvokeModel`` → NormalizedInvocation.

Both shapes are mapped onto the Chat Completions types, so the chat
normalizer does the walk. The body has no usage; token counts come from MIL.
"""

from __future__ import annotations

from ...config_base import BaseConfig
from ..normalized.types import NormalizedInvocation
from ..openai.chat.normalize import to_normalized
from ..openai.chat.schema import ChatChoice, ChatCompletion, ChatMessage, ChatRequest
from .schema import (
    DeepSeekPromptRequest,
    DeepSeekRequest,
    DeepSeekResponse,
    DeepSeekStream,
    accumulate_stream,
)


async def deepseek_to_normalized_invocation(
    request: DeepSeekRequest, response: DeepSeekResponse, *, config: BaseConfig
) -> NormalizedInvocation:
    del config
    return to_normalized(_chat_request(request), _chat_completion(response))


async def deepseek_stream_to_normalized_invocation(
    request: DeepSeekRequest, response: DeepSeekStream, *, config: BaseConfig
) -> NormalizedInvocation:
    del config
    return to_normalized(_chat_request(request), _chat_completion(accumulate_stream(response)))


def _chat_request(request: DeepSeekRequest) -> ChatRequest:
    if isinstance(request, DeepSeekPromptRequest):
        return ChatRequest(messages=[ChatMessage(role="user", content=request.prompt)])
    return request


def _chat_completion(response: DeepSeekResponse | None) -> ChatCompletion:
    choices = []
    for choice in (response.choices if response else [])[:1]:
        message = choice.message or ChatMessage(role="assistant")
        choices.append(
            ChatChoice(
                finish_reason=choice.stop_reason,
                message=message.model_copy(
                    update={"content": choice.text if choice.text is not None else message.content}
                ),
            )
        )
    return ChatCompletion(object="chat.completion", id="", choices=choices)
