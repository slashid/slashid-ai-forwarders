"""Gemini ``generateContent`` request+response → canonical NormalizedInvocation.

Public API: ``to_normalized_invocation(request, response, *, config)`` —
a joint translate that walks both halves into their canonical shape AND
populates ``accessed_files`` via ``attachments.extract_attachments`` for
``inlineData`` / ``fileData`` parts. Conforms to the ``_ToInvocation[TIn,
TOut]`` Protocol shipped in ``bedrock/mil_normalize.py`` so the same
dispatcher shape hosts Gemini too.

Key Gemini-specific behaviours:

- ``systemInstruction`` → prepended as an index-0 ``NormalizedMessage``
  with ``role="system"``, matching the canonical convention.
- Role rename: ``"model"`` → canonical ``"assistant"`` on both the
  request-side history walk and the response-side candidate content.
- ``tool_use_id`` synthesis: Gemini's ``functionCall`` emits no
  correlation id. Each call gets a stable id derived from
  ``sha256(name || json(args) || turn_index || part_index)`` where
  ``turn_index`` is the position of the model turn in the flattened
  message list (including any prepended system message) and
  ``part_index`` is the position within that turn's ``parts[]``. A
  ``functionResponse`` in a later user turn is paired to the queue of
  ``functionCall`` ids emitted so far under the same name (FIFO) — this
  survives same-name parallel calls (e.g. two ``get_weather`` calls in
  one model turn each paired to its own response in the next user
  turn).
- Server-side ``executableCode`` / ``codeExecutionResult`` map to
  ``tool_executor="server"`` blocks with the synthetic ``"code_execution"``
  tool name. ``googleSearch`` / ``googleSearchRetrieval`` grounding is
  skipped in v1 — no canonical field.
- Token counts and ``stop_reason`` stay off ``NormalizedInvocation``:
  the ``EventEnvelope`` owns those (see ``event_envelope.vertex_envelope``
  in the Vertex forwarder).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Literal

from ...config_base import BaseConfig
from ..normalized.tools import build_tools_declared
from ..normalized.types import (
    NormalizedContent,
    NormalizedInvocation,
    NormalizedInvocationInput,
    NormalizedInvocationOutput,
    NormalizedMessage,
)
from .attachments import extract_attachments
from .schema import (
    GeminiCandidate,
    GeminiCodeExecutionResultPart,
    GeminiExecutableCodePart,
    GeminiFunctionCallPart,
    GeminiFunctionResponsePart,
    GeminiPart,
    GeminiRequestBody,
    GeminiResponse,
    GeminiTextPart,
)
from .stop_reasons import STOP_REASONS

_CODE_EXECUTION_TOOL_NAME = "code_execution"


async def to_normalized_invocation(
    request: GeminiRequestBody,
    response: GeminiResponse,
    *,
    config: BaseConfig,
) -> NormalizedInvocation:
    """Map a Gemini generateContent request+response pair → canonical NormalizedInvocation.

    Populates ``accessed_files`` with inlineData / fileData attachment
    entries extracted from the fresh-turn user messages. Inline base64
    entries are hashed synchronously; ``fileData`` (``gs://``) entries
    are stubbed until a later phase adds GCS fetch.
    """
    input_side = _to_input(request)
    output_side = _to_output(response, output_turn_index=len(input_side.messages))
    accessed_files = await extract_attachments(request, config=config)
    return NormalizedInvocation(
        input=input_side,
        output=output_side,
        accessed_files=accessed_files,
    )


def _to_input(request: GeminiRequestBody) -> NormalizedInvocationInput:
    """Walk request body → NormalizedInvocationInput.

    Prepends system-instruction text as an index-0 ``NormalizedMessage``
    with ``role="system"``. Then walks ``contents[]`` in order,
    populating a ``functionCall`` queue keyed by name so
    ``functionResponse`` entries in later user turns correlate to their
    paired call via the synthesized id.
    """
    messages: list[NormalizedMessage] = []

    if request.systemInstruction and request.systemInstruction.parts:
        system_text = "".join(
            p.text for p in request.systemInstruction.parts if isinstance(p, GeminiTextPart)
        )
        messages.append(
            NormalizedMessage(
                role="system",
                content=[NormalizedContent(kind="text", text=system_text)],
            )
        )

    # tool_use_id correlation state — one FIFO of synthetic ids per tool
    # name, populated by every ``tool_use``-shaped part (functionCall AND
    # executableCode, keyed by the synthetic ``_CODE_EXECUTION_TOOL_NAME``).
    # Result-shaped parts (functionResponse / codeExecutionResult) pop the
    # queue to correlate. Same primitive for both — see ``_pair_result``.
    calls_by_name: dict[str, list[str]] = {}
    call_consumed: dict[str, int] = {}

    # Position offset for tool_use_id turn indexing — must account for the
    # prepended system message so the hash matches when a later request
    # replays the model's functionCall from response-side context.
    system_offset = len(messages)

    for turn_idx, content in enumerate(request.contents):
        canonical_turn_idx = system_offset + turn_idx
        role: Literal["user", "assistant"] = "assistant" if content.role == "model" else "user"
        blocks = _translate_parts(
            content.parts,
            turn_idx=canonical_turn_idx,
            calls_by_name=calls_by_name,
            call_consumed=call_consumed,
        )
        messages.append(NormalizedMessage(role=role, content=blocks))

    tools_declared, tool_servers = build_tools_declared(_iter_gemini_tool_specs(request))
    return NormalizedInvocationInput(
        messages=messages,
        tools_declared=tools_declared,
        tool_servers=tool_servers,
    )


def _translate_parts(
    parts: list[GeminiPart],
    *,
    turn_idx: int,
    calls_by_name: dict[str, list[str]],
    call_consumed: dict[str, int],
) -> list[NormalizedContent]:
    """Walk a ``parts[]`` list into canonical NormalizedContent blocks.

    Shared between request-side turn walks and response-side candidate
    walks: the same match dispatch handles every variant. Both call
    sites bring their own ``(calls_by_name, call_consumed)`` state —
    the request walk shares state across turns (functionCall in a model
    turn pairs with functionResponse in a later user turn), the
    response walk uses a fresh pair scoped to the single candidate.

    ``functionResponse`` appearing on the response side is unexpected
    (Gemini emits them only user-side), but if the model produces one,
    the queue-based pairing gracefully falls through to the orphan
    fallback via ``_pair_result``.
    """
    blocks: list[NormalizedContent] = []
    for part_idx, part in enumerate(parts):
        match part:
            case GeminiTextPart():
                blocks.append(NormalizedContent(kind="text", text=part.text))
            case GeminiFunctionCallPart():
                fc = part.functionCall
                sid = _synthesize_and_enqueue_call(
                    name=fc.name,
                    args=fc.args,
                    canonical_turn_idx=turn_idx,
                    part_idx=part_idx,
                    calls_by_name=calls_by_name,
                )
                blocks.append(
                    NormalizedContent(
                        kind="tool_use",
                        tool_use_id=sid,
                        tool_name=fc.name,
                        tool_input=fc.args if fc.args is not None else {},
                        tool_executor="client",
                    )
                )
            case GeminiFunctionResponsePart():
                fr = part.functionResponse
                sid = _pair_result(
                    name=fr.name,
                    canonical_turn_idx=turn_idx,
                    part_idx=part_idx,
                    calls_by_name=calls_by_name,
                    call_consumed=call_consumed,
                )
                blocks.append(
                    NormalizedContent(
                        kind="tool_result",
                        tool_use_id=sid,
                        tool_output=fr.response,
                        tool_is_error=False,
                        tool_executor="client",
                    )
                )
            case GeminiExecutableCodePart():
                ec = part.executableCode
                exec_args = {"language": ec.language, "code": ec.code}
                sid = _synthesize_and_enqueue_call(
                    name=_CODE_EXECUTION_TOOL_NAME,
                    args=exec_args,
                    canonical_turn_idx=turn_idx,
                    part_idx=part_idx,
                    calls_by_name=calls_by_name,
                )
                blocks.append(
                    NormalizedContent(
                        kind="tool_use",
                        tool_use_id=sid,
                        tool_name=_CODE_EXECUTION_TOOL_NAME,
                        tool_input=exec_args,
                        tool_executor="server",
                    )
                )
            case GeminiCodeExecutionResultPart():
                cer = part.codeExecutionResult
                sid = _pair_result(
                    name=_CODE_EXECUTION_TOOL_NAME,
                    canonical_turn_idx=turn_idx,
                    part_idx=part_idx,
                    calls_by_name=calls_by_name,
                    call_consumed=call_consumed,
                )
                blocks.append(
                    NormalizedContent(
                        kind="tool_result",
                        tool_use_id=sid,
                        tool_output={"outcome": cer.outcome, "output": cer.output},
                        tool_is_error=bool(cer.outcome) and cer.outcome != "OUTCOME_OK",
                        tool_executor="server",
                    )
                )
            # GeminiInlineDataPart / GeminiFileDataPart: attachments ride on
            # normalized.accessed_files (see attachments.extract_attachments).
            # GeminiThoughtSignaturePart / GeminiUnknownPart: skipped for v1.
    return blocks


def _to_output(
    response: GeminiResponse,
    *,
    output_turn_index: int,
) -> NormalizedInvocationOutput:
    """Walk response.candidates[0] → NormalizedInvocationOutput.

    ``output_turn_index`` = ``len(input.messages)`` (position the response
    would occupy if appended). Used for the ``functionCall`` id synthesis
    so a follow-up request replaying the same functionCall part gets an
    identical id.
    """
    if not response.candidates:
        return NormalizedInvocationOutput()

    cand: GeminiCandidate = response.candidates[0]
    stop_reason = STOP_REASONS.get(cand.finishReason or "", "unknown")

    if cand.content is None:
        return NormalizedInvocationOutput(stop_reason=stop_reason)

    # Response walk uses a fresh queue — the only within-response pairing
    # opportunity is executableCode → codeExecutionResult in one candidate.
    blocks = _translate_parts(
        cand.content.parts,
        turn_idx=output_turn_index,
        calls_by_name={},
        call_consumed={},
    )
    # Response-side content.role is always "model" — canonical is "assistant".
    return NormalizedInvocationOutput(
        message=NormalizedMessage(role="assistant", content=blocks),
        stop_reason=stop_reason,
    )


def _iter_gemini_tool_specs(
    request: GeminiRequestBody,
) -> Iterable[tuple[str, str | None, dict | None]]:
    """Yield ``(raw_name, description, parameters)`` triples for each
    ``tools[].functionDeclarations[]`` entry.

    ``build_tools_declared`` will parse names via ``parse_tool_name`` —
    Gemini tool names are typically bare (``get_weather``); the
    ``mcp__server__tool`` convention isn't native to Gemini but the
    parser handles both.
    """
    for tool in request.tools:
        for decl in tool.functionDeclarations:
            yield decl.name, decl.description, decl.parameters


def _synthesize_and_enqueue_call(
    *,
    name: str,
    args: object,
    canonical_turn_idx: int,
    part_idx: int,
    calls_by_name: dict[str, list[str]],
) -> str:
    """Synthesize a stable id for a call-shaped part and push it onto
    the pairing queue for the tool name.

    Used for both ``functionCall`` and ``executableCode`` — the pairing
    primitive is uniform across client-side and server-side tool uses.
    """
    sid = _synthesize_tool_use_id(
        name=name,
        args=args,
        turn_index=canonical_turn_idx,
        part_index=part_idx,
    )
    calls_by_name.setdefault(name, []).append(sid)
    return sid


def _pair_result(
    *,
    name: str,
    canonical_turn_idx: int,
    part_idx: int,
    calls_by_name: dict[str, list[str]],
    call_consumed: dict[str, int],
) -> str:
    """Correlate a result-shaped part (``functionResponse`` /
    ``codeExecutionResult``) to the next unconsumed call-shaped part
    with the same ``name`` in FIFO order.

    Handles the common well-formed case (one result per prior call, in
    order). Orphan results (no matching prior call, or already-consumed
    queue) synthesize a fallback id from the result's own position —
    unique but non-correlating. The events.py ``used_tools`` extractor
    treats the missing correlation as a dropped entry, matching
    Bedrock's behaviour for orphan tool_results.
    """
    queue = calls_by_name.get(name, [])
    idx = call_consumed.get(name, 0)
    if idx < len(queue):
        call_consumed[name] = idx + 1
        return queue[idx]
    return _synthesize_tool_use_id(
        name=name,
        args=None,
        turn_index=canonical_turn_idx,
        part_index=part_idx,
    )


def _synthesize_tool_use_id(
    *,
    name: str,
    args: object,
    turn_index: int,
    part_index: int,
) -> str:
    """Stable ``tool_use_id`` from ``(name, args, turn_index, part_index)``.

    Sha256 truncated to 16 hex chars — same width Bedrock's
    catalog-derived ids use for consistency. Deterministic across
    request/response walks in one call AND across follow-up requests that
    replay the same functionCall in ``contents[]`` (documented via
    ``test_gemini_tool_use_id_stability``).
    """
    payload = json.dumps(
        {"name": name, "args": args, "turn_index": turn_index, "part_index": part_index},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return "gemini-" + hashlib.sha256(payload).hexdigest()[:16]
