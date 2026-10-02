"""Table-driven MIL record dispatcher producing NormalizedInvocation.

Each ``_FORMATS`` entry pairs a request-side TypeAdapter with a
response-side TypeAdapter and a joint ``to_invocation`` translate.
BOTH must validate for a format to match — this makes ``parsed_as``
honestly reflect whether we understood the record.

``on_parse`` callbacks fire after both parses succeed and receive
``(record, request, response)`` — extended from Phase 1.1's
``(record, response)`` so callbacks can read request-side data. Today
only token accounting uses it; ``request`` is present for future
extensions.

``normalize_record(record, *, config)`` returns the canonical shape (or
an empty ``NormalizedInvocation()`` on fallthrough) and additionally
sets ``record["_parsed_as"]`` so ``bedrock_envelope`` can read it. Async —
Converse's ``to_invocation`` may issue concurrent S3 attachment fetches.
The config propagates through the dispatcher; vendor formats that don't
currently model attachments accept it for signature parity and ignore it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import TypeAdapter, ValidationError
from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    extract_stream_usage,
    message_to_normalized_invocation,
    stream_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicStreamEvent,
    AnthropicUsage,
)
from slashid_ai_forwarder_core.normalize.converse.normalize import (
    to_normalized_invocation as converse_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseRequestBody,
    ConverseResponse,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.normalize.openai.responses.normalize import (
    responses_stream_to_normalized_invocation,
    responses_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    Response,
    ResponsesRequest,
    ResponseStream,
    ResponseStreamEvent,
    ResponsesUsage,
    final_response,
)
from slashid_ai_forwarder_core.normalize.openai.usage import responses_usage_to_tokens

log = logging.getLogger(__name__)


# Vendor ``to_invocation`` signature: takes the parsed request + response
# plus a ``config: BaseConfig`` kwarg so Converse can populate
# ``accessed_files``. Anthropic vendor variants ignore ``config`` today —
# they preserve the signature for dispatch uniformity. Generic Protocol
# (not a plain ``Callable``) preserves keyword-only ``config`` semantics
# alongside the ``[TIn, TOut]`` parameterization on ``_Format``.
class _ToInvocation[TIn, TOut](Protocol):
    async def __call__(
        self,
        request: TIn,
        response: TOut,
        *,
        config: BaseConfig,
    ) -> NormalizedInvocation: ...


@dataclass(frozen=True)
class _Format[TIn, TOut]:
    """One row in the MIL format-dispatch table.

    ``on_parse`` fires after both request and response parse successfully
    and before ``to_invocation`` — mutation hook for envelope-level side
    effects (token backfill).
    """

    name: str
    request_adapter: TypeAdapter[TIn]
    response_adapter: TypeAdapter[TOut]
    to_invocation: _ToInvocation[TIn, TOut]
    on_parse: Callable[[dict[str, Any], TIn, TOut], None] | None = None


def _on_anthropic_message_parse(
    rec: dict[str, Any],
    request: AnthropicRequestBody,
    msg: AnthropicMessage,
) -> None:
    """Anthropic-message envelope side effects: backfill tokens onto the MIL record."""
    del request  # unused today; kept for signature-conformance
    _backfill_tokens_from_usage(rec, msg.usage)


def _on_anthropic_stream_parse(
    rec: dict[str, Any],
    request: AnthropicRequestBody,
    events: list[AnthropicStreamEvent],
) -> None:
    """Anthropic-stream envelope side effects: backfill tokens onto the MIL record."""
    del request
    _backfill_tokens_from_usage(rec, extract_stream_usage(events))


def _on_openai_response_parse(
    rec: dict[str, Any],
    request: ResponsesRequest,
    response: Response,
) -> None:
    del request
    _overwrite_tokens_from_responses_usage(rec, response.usage)


def _on_openai_stream_parse(
    rec: dict[str, Any],
    request: ResponsesRequest,
    events: list[ResponseStreamEvent],
) -> None:
    del request
    response = final_response(events)
    _overwrite_tokens_from_responses_usage(rec, response.usage if response else None)


_FORMATS: list[_Format] = [  # type: ignore[type-arg]  # heterogeneous [TIn, TOut] pairs
    _Format(
        name="anthropic-message",
        request_adapter=TypeAdapter(AnthropicRequestBody),
        response_adapter=TypeAdapter(AnthropicMessage),
        to_invocation=message_to_normalized_invocation,
        on_parse=_on_anthropic_message_parse,
    ),
    _Format(
        name="anthropic-stream",
        request_adapter=TypeAdapter(AnthropicRequestBody),
        response_adapter=TypeAdapter(list[AnthropicStreamEvent]),
        to_invocation=stream_to_normalized_invocation,
        on_parse=_on_anthropic_stream_parse,
    ),
    _Format(
        name="bedrock-converse",
        request_adapter=TypeAdapter(ConverseRequestBody),
        response_adapter=TypeAdapter(ConverseResponse),
        to_invocation=converse_to_normalized_invocation,
    ),
    _Format(
        name="openai-responses",
        request_adapter=TypeAdapter(ResponsesRequest),
        response_adapter=TypeAdapter(Response),
        to_invocation=responses_to_normalized_invocation,
        on_parse=_on_openai_response_parse,
    ),
    _Format(
        name="openai-responses-stream",
        request_adapter=TypeAdapter(ResponsesRequest),
        response_adapter=TypeAdapter(ResponseStream),
        to_invocation=responses_stream_to_normalized_invocation,
        on_parse=_on_openai_stream_parse,
    ),
]


async def normalize_record(
    record: dict[str, Any],
    *,
    config: BaseConfig,
) -> NormalizedInvocation:
    """Dispatch on the record's input+output body shapes → NormalizedInvocation.

    Sets ``record["_parsed_as"]`` to the matching format name, or
    ``"unknown"`` on fallthrough — ``bedrock_envelope`` reads this to populate
    the wire field.

    Both request and response must validate for a format to match. If either
    fails (e.g. S3 offload never landed and inputBodyJson is None, or the
    vendor sent a shape we haven't taught the schema yet), the dispatcher
    falls through and returns ``NormalizedInvocation()`` — envelope-side
    fields still emit (identity, model, tokens), only semantic detail is
    dropped.

    ``config`` propagates to the vendor ``to_invocation`` (Converse uses
    ``config.include_raw_content`` / ``config.max_content_size`` for
    attachment redaction; the Anthropic variants accept-and-ignore for
    signature parity).
    """
    in_body = (record.get("input") or {}).get("inputBodyJson")
    out_body = (record.get("output") or {}).get("outputBodyJson")

    for fmt in _FORMATS:
        try:
            parsed_out = fmt.response_adapter.validate_python(out_body)
            parsed_in = fmt.request_adapter.validate_python(in_body)
        except ValidationError:
            continue
        if fmt.on_parse is not None:
            fmt.on_parse(record, parsed_in, parsed_out)
        record["_parsed_as"] = fmt.name
        log.debug("normalized record as %s", fmt.name)
        return await fmt.to_invocation(parsed_in, parsed_out, config=config)

    log.warning(
        "unrecognized MIL body shape for model=%s request_id=%s",
        record.get("modelId"),
        record.get("requestId"),
    )
    record["_parsed_as"] = "unknown"
    return NormalizedInvocation()


def _backfill_tokens_from_usage(
    record: dict[str, Any],
    usage: AnthropicUsage | None,
) -> None:
    """Copy Anthropic body.usage counts to MIL top-level fields when missing."""
    if usage is None:
        return
    inp = record.setdefault("input", {})
    out = record.setdefault("output", {})
    _set_if_absent(inp, "inputTokenCount", usage.input_tokens)
    _set_if_absent(out, "outputTokenCount", usage.output_tokens)
    _set_if_absent(inp, "cacheReadInputTokenCount", usage.cache_read_input_tokens)
    _set_if_absent(inp, "cacheWriteInputTokenCount", usage.cache_creation_input_tokens)


def _overwrite_tokens_from_responses_usage(
    record: dict[str, Any],
    usage: ResponsesUsage | None,
) -> None:
    """Replace MIL token counts with the additive split from body.usage."""
    if usage is None:
        return
    # MIL copies OpenAI's inclusive totals; the wire model wants them disjoint.
    tokens = responses_usage_to_tokens(usage)
    inp = record.setdefault("input", {})
    out = record.setdefault("output", {})
    inp["inputTokenCount"] = tokens.input
    inp["cacheReadInputTokenCount"] = tokens.cache_read
    inp["cacheWriteInputTokenCount"] = tokens.cache_write
    out["outputTokenCount"] = tokens.output
    out["reasoningTokenCount"] = tokens.reasoning


def _set_if_absent(container: dict[str, Any], key: str, value: int | None) -> None:
    if container.get(key) is None and isinstance(value, int):
        container[key] = value
