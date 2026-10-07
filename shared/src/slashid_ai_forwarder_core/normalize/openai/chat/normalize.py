"""OpenAI Chat Completions → NormalizedInvocation.

``to_normalized`` is the pure walk; the async entry points match the
Bedrock ``_ToInvocation`` protocol and add the attachments, which may need S3. Models served
through Bedrock's OpenAI endpoint (gpt-oss) inline their reasoning as a
leading ``<reasoning>…</reasoning>`` span of the assistant text.
"""

from __future__ import annotations

import json

from pydantic import JsonValue

from ....config_base import BaseConfig
from ...normalized.tools import build_tools_declared
from ...normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)
from ..data_url import decode_data_url
from ..merge import Role, merge_same_role
from ..stop_reasons import chat_stop_reason
from ..usage import chat_usage_to_tokens
from .attachments import extract_attachments
from .schema import (
    ChatCompletion,
    ChatFilePart,
    ChatImagePart,
    ChatMessage,
    ChatPart,
    ChatRequest,
    ChatStream,
    ChatTextPart,
    ChatToolCall,
    accumulate_stream,
)

_REASONING_OPEN = "<reasoning>"
_REASONING_CLOSE = "</reasoning>"


def to_normalized(request: ChatRequest, response: ChatCompletion) -> NormalizedInvocation:
    choice = response.choices[0] if response.choices else None
    return NormalizedInvocation(
        input=_request_to_input(request),
        output=NormalizedInvocationOutput(
            message=_output_message(choice.message) if choice else None,
            stop_reason=chat_stop_reason(choice.finish_reason if choice else None),
        ),
        tokens=chat_usage_to_tokens(response.usage),
    )


async def chat_to_normalized_invocation(
    request: ChatRequest, response: ChatCompletion, *, config: BaseConfig
) -> NormalizedInvocation:
    normalized = to_normalized(request, response)
    normalized.accessed_files = await extract_attachments(request, config=config)
    return normalized


async def chat_stream_to_normalized_invocation(
    request: ChatRequest, response: ChatStream, *, config: BaseConfig
) -> NormalizedInvocation:
    final = accumulate_stream(response)
    normalized = (
        NormalizedInvocation(input=_request_to_input(request))
        if final is None
        else to_normalized(request, final)
    )
    normalized.accessed_files = await extract_attachments(request, config=config)
    return normalized


def _request_to_input(request: ChatRequest) -> NormalizedInvocationInput:
    mapped = [m for message in request.messages if (m := _message(message)) is not None]
    tools_declared, tool_servers = build_tools_declared(
        (
            t.function.name,
            t.function.description,
            t.function.parameters if isinstance(t.function.parameters, dict) else None,
        )
        for t in request.tools
        if t.function is not None
    )
    return NormalizedInvocationInput(
        messages=merge_same_role(mapped), tools_declared=tools_declared, tool_servers=tool_servers
    )


def _message(message: ChatMessage) -> tuple[Role, list[NormalizedContent]] | None:
    match message.role:
        case "system" | "developer":
            blocks = _parts(message.content)
            role: Role = "system"
        case "assistant":
            blocks = _assistant_blocks(message)
            role = "assistant"
        case "tool":
            blocks = [_tool_result(message)]
            role = "user"
        case _:
            blocks = _parts(message.content)
            role = "user"
    return (role, blocks) if blocks else None


def _output_message(message: ChatMessage) -> NormalizedMessage | None:
    blocks = _assistant_blocks(message)
    return NormalizedMessage(role="assistant", content=blocks) if blocks else None


def _assistant_blocks(message: ChatMessage) -> list[NormalizedContent]:
    blocks = _parts(message.content, split_reasoning=True)
    if message.reasoning_content:
        blocks.insert(0, NormalizedContent(kind="reasoning", text=message.reasoning_content))
    if message.refusal:
        blocks.append(NormalizedContent(kind="text", text=message.refusal))
    blocks.extend(_tool_use(call) for call in message.tool_calls)
    return blocks


def _tool_use(call: ChatToolCall) -> NormalizedContent:
    return NormalizedContent(
        kind="tool_use",
        tool_use_id=call.id,
        tool_name=call.function.name,
        tool_input=_json_or_raw(call.function.arguments),
        tool_executor="client",
    )


def _tool_result(message: ChatMessage) -> NormalizedContent:
    output: JsonValue = (
        message.content
        if isinstance(message.content, str) or message.content is None
        else [p.model_dump(mode="json") for p in message.content]
    )
    return NormalizedContent(
        kind="tool_result",
        tool_use_id=message.tool_call_id,
        tool_output=output,
        tool_executor="client",
    )


def _json_or_raw(arguments: str) -> JsonValue:
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


def _parts(
    content: str | list[ChatPart] | None, *, split_reasoning: bool = False
) -> list[NormalizedContent]:
    if content is None:
        return []
    if isinstance(content, str):
        return _text(content, split_reasoning=split_reasoning)
    out: list[NormalizedContent] = []
    for part in content:
        match part:
            case ChatTextPart():
                out.extend(_text(part.text, split_reasoning=split_reasoning))
            case ChatImagePart():
                media_type, byte_length = decode_data_url(part.image_url.url)
                out.append(
                    NormalizedContent(kind="image", media_type=media_type, byte_length=byte_length)
                )
            case ChatFilePart():
                media_type, byte_length = decode_data_url(part.file.file_data)
                out.append(
                    NormalizedContent(
                        kind="document", media_type=media_type, byte_length=byte_length
                    )
                )
    return out


def _text(text: str, *, split_reasoning: bool) -> list[NormalizedContent]:
    if not text.strip():
        return []
    if split_reasoning and text.startswith(_REASONING_OPEN):
        reasoning, _, rest = text.removeprefix(_REASONING_OPEN).partition(_REASONING_CLOSE)
        blocks = [NormalizedContent(kind="reasoning", text=reasoning)]
        if rest.strip():
            blocks.append(NormalizedContent(kind="text", text=rest))
        return blocks
    return [NormalizedContent(kind="text", text=text)]
