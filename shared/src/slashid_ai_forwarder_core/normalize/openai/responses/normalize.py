"""OpenAI Responses API → NormalizedInvocation.

``to_normalized`` is the pure walk; the async entry points match the
Bedrock ``_ToInvocation`` protocol and ignore ``config``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Literal

from pydantic import JsonValue
from pydantic_extra_types.mime_types import MimeType

from ....config_base import BaseConfig
from ...normalized.media_types import parse_media_type
from ...normalized.tools import build_tools_declared
from ...normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)
from ..stop_reasons import responses_stop_reason
from ..usage import responses_usage_to_tokens
from .schema import (
    Response,
    ResponsesCompaction,
    ResponsesCustomToolCall,
    ResponsesCustomToolCallOutput,
    ResponsesFunctionCall,
    ResponsesFunctionCallOutput,
    ResponsesInputImage,
    ResponsesInputText,
    ResponsesItem,
    ResponsesMessage,
    ResponsesOutputText,
    ResponsesPart,
    ResponsesReasoning,
    ResponsesRequest,
    ResponseStreamEvent,
    ResponsesWebSearchCall,
    final_response,
)

_Role = Literal["system", "user", "assistant"]


def to_normalized(request: ResponsesRequest, response: Response) -> NormalizedInvocation:
    return NormalizedInvocation(
        input=_request_to_input(request),
        output=_response_to_output(response),
        tokens=responses_usage_to_tokens(response.usage),
    )


async def responses_to_normalized_invocation(
    request: ResponsesRequest, response: Response, *, config: BaseConfig
) -> NormalizedInvocation:
    del config
    return to_normalized(request, response)


async def responses_stream_to_normalized_invocation(
    request: ResponsesRequest, events: list[ResponseStreamEvent], *, config: BaseConfig
) -> NormalizedInvocation:
    del config
    response = final_response(events)
    if response is None:
        return NormalizedInvocation(input=_request_to_input(request))
    return to_normalized(request, response)


def _request_to_input(request: ResponsesRequest) -> NormalizedInvocationInput:
    mapped: list[tuple[_Role, list[NormalizedContent]]] = []
    if request.instructions is not None:
        mapped.append(("system", [NormalizedContent(kind="text", text=request.instructions)]))
    if isinstance(request.input, str):
        mapped.append(("user", [NormalizedContent(kind="text", text=request.input)]))
    else:
        mapped.extend(m for item in request.input if (m := _item(item)) is not None)
    tools_declared, tool_servers = build_tools_declared(
        (t.name or t.type, t.description, t.parameters if isinstance(t.parameters, dict) else None)
        for t in request.tools
    )
    return NormalizedInvocationInput(
        messages=_merge(mapped), tools_declared=tools_declared, tool_servers=tool_servers
    )


def _merge(mapped: list[tuple[_Role, list[NormalizedContent]]]) -> list[NormalizedMessage]:
    """Consecutive same-role items share a message; a compaction is always alone,
    so it never folds the round before it into its own."""
    groups: list[tuple[_Role, list[NormalizedContent]]] = []
    for role, blocks in mapped:
        if (
            groups
            and groups[-1][0] == role
            and not (_is_compaction(blocks) or _is_compaction(groups[-1][1]))
        ):
            groups[-1][1].extend(blocks)
        else:
            groups.append((role, list(blocks)))
    return [NormalizedMessage(role=role, content=blocks) for role, blocks in groups]


def _is_compaction(blocks: list[NormalizedContent]) -> bool:
    return any(b.kind == "compaction" for b in blocks)


def _response_to_output(response: Response) -> NormalizedInvocationOutput:
    blocks = [b for item in response.output if (m := _item(item)) is not None for b in m[1]]
    has_tool_call = any(
        isinstance(i, ResponsesFunctionCall | ResponsesCustomToolCall) for i in response.output
    )
    details = response.incomplete_details
    return NormalizedInvocationOutput(
        message=NormalizedMessage(role="assistant", content=blocks) if blocks else None,
        stop_reason=responses_stop_reason(
            response.status, details.reason if details else None, has_tool_call=has_tool_call
        ),
    )


def _item(item: ResponsesItem) -> tuple[_Role, list[NormalizedContent]] | None:
    match item:
        case ResponsesMessage():
            blocks = _parts(item.content)
            if not blocks:
                return None
            return _role(item.role), blocks
        case ResponsesFunctionCall():
            return "assistant", [_tool_use(item.call_id, item.name, _json_or_raw(item.arguments))]
        case ResponsesCustomToolCall():
            return "assistant", [_tool_use(item.call_id, item.name, item.input)]
        case ResponsesFunctionCallOutput() | ResponsesCustomToolCallOutput():
            output: JsonValue = (
                item.output
                if isinstance(item.output, str)
                else [p.model_dump(mode="json") for p in item.output]
            )
            block = NormalizedContent(
                kind="tool_result",
                tool_use_id=item.call_id,
                tool_output=output,
                tool_executor="client",
            )
            return "user", [block]
        case ResponsesReasoning():
            text = "\n".join(s.text for s in item.summary) or None
            return "assistant", [NormalizedContent(kind="reasoning", text=text)]
        case ResponsesWebSearchCall():
            return "assistant", [
                NormalizedContent(
                    kind="tool_use",
                    tool_use_id=item.id,
                    tool_name="web_search",
                    tool_input=item.action,
                    tool_executor="server",
                )
            ]
        case ResponsesCompaction():
            digest = hashlib.sha256((item.encrypted_content or item.id or "").encode()).hexdigest()
            return "assistant", [NormalizedContent(kind="compaction", text=digest)]
    return None


def _role(role: str) -> _Role:
    match role:
        case "system" | "developer":
            return "system"
        case "assistant":
            return "assistant"
    return "user"


def _tool_use(call_id: str, name: str, tool_input: JsonValue) -> NormalizedContent:
    return NormalizedContent(
        kind="tool_use",
        tool_use_id=call_id,
        tool_name=name,
        tool_input=tool_input,
        tool_executor="client",
    )


def _json_or_raw(arguments: str) -> JsonValue:
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


def _parts(content: str | list[ResponsesPart]) -> list[NormalizedContent]:
    if isinstance(content, str):
        return [NormalizedContent(kind="text", text=content)]
    out: list[NormalizedContent] = []
    for part in content:
        match part:
            case ResponsesInputText() | ResponsesOutputText():
                out.append(NormalizedContent(kind="text", text=part.text))
            case ResponsesInputImage():
                media_type, byte_length = _data_url(part.image_url)
                out.append(
                    NormalizedContent(kind="image", media_type=media_type, byte_length=byte_length)
                )
    return out


def _data_url(url: str | None) -> tuple[MimeType | None, int | None]:
    """``(media_type, byte_length)`` of a base64 ``data:`` URL; both ``None`` otherwise."""
    header, sep, payload = (url or "").partition(",")
    if not sep or not header.startswith("data:") or not header.endswith(";base64"):
        return None, None
    try:
        data = base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None, None
    media_type = parse_media_type(header.removeprefix("data:").removesuffix(";base64"))
    return media_type, len(data)
