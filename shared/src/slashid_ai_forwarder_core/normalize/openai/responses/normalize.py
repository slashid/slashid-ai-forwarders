"""OpenAI Responses API → NormalizedInvocation.

``to_normalized`` is the pure walk; the async entry points match the
Bedrock ``_ToInvocation`` protocol and ignore ``config``.
"""

from __future__ import annotations

import hashlib
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
    request: ResponsesRequest, response: list[ResponseStreamEvent], *, config: BaseConfig
) -> NormalizedInvocation:
    del config
    final = final_response(response)
    if final is None:
        return NormalizedInvocation(input=_request_to_input(request))
    return to_normalized(request, final)


def _request_to_input(request: ResponsesRequest) -> NormalizedInvocationInput:
    mapped: list[tuple[Role, list[NormalizedContent]]] = []
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
        messages=merge_same_role(mapped), tools_declared=tools_declared, tool_servers=tool_servers
    )


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


def _item(item: ResponsesItem) -> tuple[Role, list[NormalizedContent]] | None:
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


def _role(role: str) -> Role:
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
                media_type, byte_length = decode_data_url(part.image_url)
                out.append(
                    NormalizedContent(kind="image", media_type=media_type, byte_length=byte_length)
                )
    return out
