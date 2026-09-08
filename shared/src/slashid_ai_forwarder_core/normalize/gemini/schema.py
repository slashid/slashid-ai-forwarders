"""Pydantic schemas for Google Vertex AI Gemini ``generateContent`` wire shapes.

Content parts inside ``GeminiContent.parts[]`` are key-tagged (one of
``text`` / ``functionCall`` / ``functionResponse`` / ``inlineData`` /
``fileData`` / ``executableCode`` / ``codeExecutionResult`` /
``thoughtSignature`` as the single top-level key). Modeled via
``_StrictModel`` variants so pydantic's smart-union picks the variant
whose key IS present; ``GeminiUnknownPart`` is the catch-all fallback.

Reference: Google Cloud Vertex AI GenerateContent API (google.cloud.aiplatform.v1)
    https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/projects.locations.publishers.models/generateContent
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue
from pydantic.json_schema import JsonSchemaValue

from .._base import _LenientModel, _StrictModel

# --------------------------------------------------------------------------
# Part payload types — the inner shapes for the key-tagged variants below.
# --------------------------------------------------------------------------


class GeminiFunctionCall(_LenientModel):
    """Model-side function-call request — inside ``functionCall`` parts.

    ``args`` is the tool input; ``name`` is the raw function name declared
    in ``tools[].functionDeclarations[].name``. Gemini emits no correlation
    id — the normalizer synthesizes one via a stable hash of
    ``(name, args, turn_index, part_index)``.
    """

    name: str
    args: JsonValue = None


class GeminiFunctionResponse(_LenientModel):
    """Client-side function-call result — inside ``functionResponse`` parts.

    ``name`` echoes the ``functionCall.name`` this responds to;
    ``response`` is the tool output payload (arbitrary JSON).
    """

    name: str
    response: JsonValue = None


class GeminiBlob(_LenientModel):
    """Inline binary attachment — inside ``inlineData`` parts.

    ``data`` is base64-encoded wire bytes; ``mimeType`` is IANA
    (e.g. ``image/png``, ``application/pdf``).
    """

    mimeType: str | None = None
    data: str  # base64-encoded


class GeminiFileData(_LenientModel):
    """Remote attachment reference — inside ``fileData`` parts.

    ``fileUri`` is a ``gs://bucket/object`` URL; the referenced object
    stays in customer GCS. V1 emits a stub AIAccessedFile (no hash / no
    byte_length); a later phase adds a GCS fetch under a ``[gcs]`` extras
    group.
    """

    mimeType: str | None = None
    fileUri: str


class GeminiExecutableCode(_LenientModel):
    """Server-side executable-code request — inside ``executableCode`` parts."""

    language: str | None = None
    code: str


class GeminiCodeExecutionResult(_LenientModel):
    """Server-side code-execution result — inside ``codeExecutionResult`` parts."""

    outcome: str | None = None
    output: str | None = None


# --------------------------------------------------------------------------
# Content parts — key-tagged discriminated union
# ORDER MATTERS: pydantic smart-union picks the first strict variant that
# validates; GeminiUnknownPart is last and always accepts.
# --------------------------------------------------------------------------


class GeminiTextPart(_StrictModel):
    text: str


class GeminiFunctionCallPart(_StrictModel):
    functionCall: GeminiFunctionCall


class GeminiFunctionResponsePart(_StrictModel):
    functionResponse: GeminiFunctionResponse


class GeminiInlineDataPart(_StrictModel):
    inlineData: GeminiBlob


class GeminiFileDataPart(_StrictModel):
    fileData: GeminiFileData


class GeminiExecutableCodePart(_StrictModel):
    executableCode: GeminiExecutableCode


class GeminiCodeExecutionResultPart(_StrictModel):
    codeExecutionResult: GeminiCodeExecutionResult


class GeminiThoughtSignaturePart(_StrictModel):
    """Model-side reasoning signature — carries opaque state for
    context-continuation across a multi-turn thinking flow. V1 skips it
    entirely (no canonical field yet)."""

    thoughtSignature: str


class GeminiUnknownPart(_LenientModel):
    """Catch-all for keys we don't model. Preserved on ``model_dump`` so
    lenient re-serialization is lossless."""


GeminiPart = (
    GeminiTextPart
    | GeminiFunctionCallPart
    | GeminiFunctionResponsePart
    | GeminiInlineDataPart
    | GeminiFileDataPart
    | GeminiExecutableCodePart
    | GeminiCodeExecutionResultPart
    | GeminiThoughtSignaturePart
    | GeminiUnknownPart
)


# --------------------------------------------------------------------------
# Content messages
# --------------------------------------------------------------------------


class GeminiContent(_LenientModel):
    """One turn in ``GenerateContentRequest.contents`` OR
    ``candidates[].content``.

    Request-side role is ``Literal["user", "model"]`` (Gemini's
    "assistant" is spelled ``"model"``). Response-side ``content.role``
    is always ``"model"`` but we keep the same union for shape uniformity
    — the response normalizer renames to canonical ``"assistant"``.
    """

    role: Literal["user", "model"]
    parts: list[GeminiPart] = Field(default_factory=list)


class GeminiSystemInstruction(_LenientModel):
    """``GenerateContentRequest.systemInstruction`` — parts only, no role."""

    parts: list[GeminiPart] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Tool declaration
# --------------------------------------------------------------------------


class GeminiFunctionDeclaration(_LenientModel):
    """One function tool declaration — inside ``tools[].functionDeclarations[]``."""

    name: str
    description: str | None = None
    parameters: JsonSchemaValue | None = None


class GeminiTool(_LenientModel):
    """One entry of ``GenerateContentRequest.tools[]``.

    Function-declaration tools are the only variant Phase 3.1 populates.
    Server-side grounding variants (``googleSearch``,
    ``googleSearchRetrieval``, ``codeExecution``) are tolerated via
    ``_LenientModel`` but produce no canonical AITool — grounding lives
    outside the current tool-call model.
    """

    functionDeclarations: list[GeminiFunctionDeclaration] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Request body
# --------------------------------------------------------------------------


class GeminiGenerationConfig(_LenientModel):
    """``GenerateContentRequest.generationConfig`` — only the fields
    the normalizer actually reads.

    ``maxOutputTokens`` is used by ``resolve_finish_reason`` to
    recover the ``MAX_TOKENS`` signal on streamed responses (which
    arrive at BQ with ``finishReason: null`` after Vertex's
    server-side merge). Every other generationConfig field
    (temperature, topP, thinkingConfig, safetySettings, ...) stays
    dropped via ``_LenientModel``'s ``extra="ignore"``.
    """

    maxOutputTokens: int | None = None


class GeminiRequestBody(_LenientModel):
    """``GenerateContentRequest`` — content-relevant fields only.

    ``model``, ``safetySettings``, ``toolConfig`` all drop via
    ``_LenientModel``. ``generationConfig`` is materialized narrowly
    (only ``maxOutputTokens``) so the normalizer can key the
    streaming-stop-reason heuristic off it.
    """

    contents: list[GeminiContent] = Field(default_factory=list)
    systemInstruction: GeminiSystemInstruction | None = None
    tools: list[GeminiTool] = Field(default_factory=list)
    generationConfig: GeminiGenerationConfig | None = None


# --------------------------------------------------------------------------
# Response body
# --------------------------------------------------------------------------


class GeminiTokensDetails(_LenientModel):
    """One per-modality entry in ``usageMetadata.promptTokensDetails[]`` /
    ``candidatesTokensDetails[]``. Kept for completeness; the envelope
    only reads the top-level counts."""

    modality: str | None = None
    tokenCount: int = 0


class GeminiUsageMetadata(_LenientModel):
    """``GenerateContentResponse.usageMetadata`` — token counts.

    ``promptTokenCount``, ``candidatesTokenCount``, and
    ``thoughtsTokenCount`` map onto canonical
    ``AIInvocationTokens.input`` / ``.output`` / ``.reasoning``.
    ``cachedContentTokenCount`` maps onto ``.cache_read``; Gemini
    exposes no cache-write count.
    """

    promptTokenCount: int = 0
    candidatesTokenCount: int = 0
    thoughtsTokenCount: int = 0
    totalTokenCount: int = 0
    cachedContentTokenCount: int = 0
    promptTokensDetails: list[GeminiTokensDetails] = Field(default_factory=list)
    candidatesTokensDetails: list[GeminiTokensDetails] = Field(default_factory=list)


class GeminiCandidate(_LenientModel):
    """One entry of ``GenerateContentResponse.candidates[]``.

    Phase 3.1 reads ``candidates[0]`` only — the ``candidateCount``
    parameter defaults to 1 and multi-candidate requests are rare in
    production.
    """

    content: GeminiContent | None = None
    finishReason: str | None = None
    avgLogprobs: float | None = None
    score: float | None = None


class GeminiResponse(_LenientModel):
    """``GenerateContentResponse`` — content-relevant fields only.

    ``safetyRatings``, ``groundingMetadata``, ``promptFeedback`` all
    drop via ``_LenientModel``.
    """

    candidates: list[GeminiCandidate] = Field(default_factory=list)
    usageMetadata: GeminiUsageMetadata = Field(default_factory=GeminiUsageMetadata)
    modelVersion: str | None = None
    responseId: str | None = None
    createTime: str | None = None
