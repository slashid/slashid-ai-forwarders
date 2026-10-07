"""Pydantic schemas for the OpenAI Chat Completions wire shapes we consume."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, JsonValue

from ..._base import _LenientModel

# --------------------------------------------------------------------------
# Content parts
# --------------------------------------------------------------------------


class ChatTextPart(_LenientModel):
    type: Literal["text"]
    text: str


class ChatImageUrl(_LenientModel):
    url: str
    detail: str | None = None


class ChatImagePart(_LenientModel):
    type: Literal["image_url"]
    image_url: ChatImageUrl


class ChatFile(_LenientModel):
    filename: str | None = None
    file_data: str | None = None
    file_id: str | None = None


class ChatFilePart(_LenientModel):
    type: Literal["file"]
    file: ChatFile


class ChatUnknownPart(_LenientModel):
    type: str


ChatPart = Annotated[
    ChatTextPart | ChatImagePart | ChatFilePart | ChatUnknownPart,
    Field(union_mode="left_to_right"),
]

# --------------------------------------------------------------------------
# Messages and request
# --------------------------------------------------------------------------


class ChatFunctionCall(_LenientModel):
    name: str
    arguments: str


class ChatToolCall(_LenientModel):
    id: str
    function: ChatFunctionCall


class ChatMessage(_LenientModel):
    role: str
    content: str | list[ChatPart] | None = None
    refusal: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ChatToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class ChatFunction(_LenientModel):
    name: str
    description: str | None = None
    parameters: JsonValue = None


class ChatTool(_LenientModel):
    type: str = "function"
    function: ChatFunction | None = None


class ChatRequest(_LenientModel):
    model: str | None = None
    messages: list[ChatMessage]
    tools: list[ChatTool] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Response
# --------------------------------------------------------------------------


class ChatPromptTokensDetails(_LenientModel):
    cached_tokens: int = 0
    cache_write_tokens: int = 0


class ChatCompletionTokensDetails(_LenientModel):
    reasoning_tokens: int = 0


class ChatUsage(_LenientModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: ChatPromptTokensDetails | None = None
    completion_tokens_details: ChatCompletionTokensDetails | None = None


class ChatChoice(_LenientModel):
    finish_reason: str | None = None
    message: ChatMessage


class ChatCompletion(_LenientModel):
    object: Literal["chat.completion"]
    id: str
    choices: list[ChatChoice] = Field(default_factory=list)
    usage: ChatUsage | None = None


# --------------------------------------------------------------------------
# Streaming
# --------------------------------------------------------------------------


class ChatDeltaFunction(_LenientModel):
    name: str | None = None
    arguments: str | None = None


class ChatDeltaToolCall(_LenientModel):
    index: int = 0
    id: str | None = None
    function: ChatDeltaFunction | None = None


class ChatDelta(_LenientModel):
    role: str | None = None
    content: str | None = None
    refusal: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ChatDeltaToolCall] = Field(default_factory=list)


class ChatChunkChoice(_LenientModel):
    index: int = 0
    delta: ChatDelta = Field(default_factory=ChatDelta)
    finish_reason: str | None = None


class ChatChunk(_LenientModel):
    object: Literal["chat.completion.chunk"]
    id: str
    choices: list[ChatChunkChoice] = Field(default_factory=list)
    usage: ChatUsage | None = None


def _require_chunks(chunks: list[ChatChunk]) -> list[ChatChunk]:
    if not chunks:
        raise ValueError("no chat.completion.chunk")
    return chunks


# A non-empty list of chunks, so an empty list isn't taken for a stream.
ChatStream = Annotated[list[ChatChunk], AfterValidator(_require_chunks)]


class _ToolCallAcc:
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments = ""


class _ChoiceAcc:
    def __init__(self) -> None:
        self.role = "assistant"
        self.content = ""
        self.refusal = ""
        self.reasoning = ""
        self.finish_reason: str | None = None
        self.tool_calls: dict[int, _ToolCallAcc] = {}


def accumulate_stream(chunks: list[ChatChunk]) -> ChatCompletion | None:
    """Fold chunk deltas into the ``ChatCompletion`` a non-streaming call would return."""
    if not chunks:
        return None
    choices: dict[int, _ChoiceAcc] = {}
    usage: ChatUsage | None = None
    for chunk in chunks:
        usage = chunk.usage or usage
        for choice in chunk.choices:
            acc = choices.setdefault(choice.index, _ChoiceAcc())
            acc.role = choice.delta.role or acc.role
            acc.content += choice.delta.content or ""
            acc.refusal += choice.delta.refusal or ""
            acc.reasoning += choice.delta.reasoning_content or ""
            acc.finish_reason = choice.finish_reason or acc.finish_reason
            for call in choice.delta.tool_calls:
                tool = acc.tool_calls.setdefault(call.index, _ToolCallAcc())
                tool.id = call.id or tool.id
                if call.function is not None:
                    tool.name += call.function.name or ""
                    tool.arguments += call.function.arguments or ""
    return ChatCompletion(
        object="chat.completion",
        id=chunks[0].id,
        choices=[
            ChatChoice(
                finish_reason=acc.finish_reason,
                message=ChatMessage(
                    role=acc.role,
                    content=acc.content or None,
                    refusal=acc.refusal or None,
                    reasoning_content=acc.reasoning or None,
                    tool_calls=[
                        ChatToolCall(
                            id=t.id, function=ChatFunctionCall(name=t.name, arguments=t.arguments)
                        )
                        for _, t in sorted(acc.tool_calls.items())
                    ],
                ),
            )
            for _, acc in sorted(choices.items())
        ],
        usage=usage,
    )
