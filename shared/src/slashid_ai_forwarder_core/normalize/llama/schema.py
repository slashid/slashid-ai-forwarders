"""Pydantic schemas for the Llama native ``InvokeModel`` body: raw prompt in, ``generation`` out."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

from .._base import _LenientModel


class LlamaRequest(_LenientModel):
    prompt: str


class LlamaResponse(_LenientModel):
    generation: str
    prompt_token_count: int | None = None
    generation_token_count: int | None = None
    stop_reason: str | None = None


def _require_chunks(chunks: list[LlamaResponse]) -> list[LlamaResponse]:
    if not chunks:
        raise ValueError("no llama chunk")
    return chunks


# A non-empty list of chunks, so an empty list isn't taken for a stream.
LlamaStream = Annotated[list[LlamaResponse], AfterValidator(_require_chunks)]


def accumulate_stream(chunks: list[LlamaResponse]) -> LlamaResponse | None:
    """Fold stream chunks into the response a non-streaming call would return."""
    if not chunks:
        return None
    return LlamaResponse(
        generation="".join(c.generation for c in chunks),
        prompt_token_count=next(
            (c.prompt_token_count for c in chunks if c.prompt_token_count), None
        ),
        generation_token_count=next(
            (c.generation_token_count for c in reversed(chunks) if c.generation_token_count), None
        ),
        stop_reason=next((c.stop_reason for c in reversed(chunks) if c.stop_reason), None),
    )
