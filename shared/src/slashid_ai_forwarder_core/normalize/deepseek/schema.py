"""Pydantic schemas for the DeepSeek R1 native ``InvokeModel`` body.

The request is a chat body or a bare ``prompt``. A response carries ``choices``
with a ``message`` (chat) or ``text`` (prompt) and a ``stop_reason``, and no
``object``, ``id`` or ``usage``; stream chunks have the same shape with a
fragment in ``message`` / ``text``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, model_validator

from .._base import _LenientModel
from ..openai.chat.schema import ChatMessage, ChatRequest


class DeepSeekPromptRequest(_LenientModel):
    prompt: str


DeepSeekRequest = ChatRequest | DeepSeekPromptRequest


class DeepSeekChoice(_LenientModel):
    message: ChatMessage | None = None
    text: str | None = None
    stop_reason: str | None = None


class DeepSeekResponse(_LenientModel):
    # Always absent: a body with an ``object`` is a real chat completion.
    object: None = None
    choices: list[DeepSeekChoice]

    @model_validator(mode="after")
    def _has_an_answer_shape(self) -> DeepSeekResponse:
        if not any(c.message is not None or c.text is not None for c in self.choices):
            raise ValueError("no choice with a message or text")
        return self


def _require_chunks(chunks: list[DeepSeekResponse]) -> list[DeepSeekResponse]:
    if not chunks:
        raise ValueError("no deepseek chunk")
    return chunks


# A non-empty list of chunks, so an empty list isn't taken for a stream.
DeepSeekStream = Annotated[list[DeepSeekResponse], AfterValidator(_require_chunks)]


def accumulate_stream(chunks: list[DeepSeekResponse]) -> DeepSeekResponse | None:
    """Fold stream chunks into the response a non-streaming call would return."""
    choices = [chunk.choices[0] for chunk in chunks if chunk.choices]
    if not choices:
        return None
    messages = [c.message for c in choices if c.message is not None]
    texts = [c.text for c in choices if c.text is not None]
    return DeepSeekResponse(
        choices=[
            DeepSeekChoice(
                message=ChatMessage(
                    role=next((m.role for m in messages), "assistant"),
                    content="".join(m.content for m in messages if isinstance(m.content, str))
                    or None,
                    reasoning_content="".join(m.reasoning_content or "" for m in messages) or None,
                )
                if messages
                else None,
                text="".join(texts) if texts else None,
                stop_reason=next((c.stop_reason for c in reversed(choices) if c.stop_reason), None),
            )
        ]
    )
