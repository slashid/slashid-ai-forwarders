"""Gemini generateContent wire-schema round-trips + key-tagged discriminator.

Fixtures are lifted from real BQ request-response logging rows captured
during the Vertex POC (2026-09-04, project ``vertex-test-507702``,
``gemini-2.5-flash``). Text-only, systemInstruction + multi-turn, and
functionCall/functionResponse round-trip variants are all real; inline
data / file data / server-side executable-code variants are synthetic
(the POC didn't exercise those).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiCandidate,
    GeminiCodeExecutionResultPart,
    GeminiContent,
    GeminiExecutableCodePart,
    GeminiFileDataPart,
    GeminiFunctionCallPart,
    GeminiFunctionResponsePart,
    GeminiInlineDataPart,
    GeminiRequestBody,
    GeminiResponse,
    GeminiSystemInstruction,
    GeminiTextPart,
    GeminiThoughtSignaturePart,
    GeminiUnknownPart,
    GeminiUsageMetadata,
)

# --------------------------------------------------------------------------
# Request-side — real POC rows
# --------------------------------------------------------------------------


def test_gemini_request_round_trip_text_only() -> None:
    raw = {
        "contents": [{"parts": [{"text": "Reply with only the word ACK-A"}], "role": "user"}],
        "model": (
            "projects/vertex-test-507702/locations/us-central1"
            "/publishers/google/models/gemini-2.5-flash"
        ),
    }
    req = GeminiRequestBody.model_validate(raw)
    assert len(req.contents) == 1
    assert req.contents[0].role == "user"
    assert isinstance(req.contents[0].parts[0], GeminiTextPart)
    assert req.contents[0].parts[0].text == "Reply with only the word ACK-A"


def test_gemini_request_round_trip_system_instruction_and_multi_turn() -> None:
    raw = {
        "contents": [
            {"parts": [{"text": "what is 2+2?"}], "role": "user"},
            {"parts": [{"text": "4"}], "role": "model"},
            {"parts": [{"text": "what is 5+7?"}], "role": "user"},
        ],
        "systemInstruction": {
            "parts": [{"text": "You are terse. Reply with one integer, nothing else."}]
        },
    }
    req = GeminiRequestBody.model_validate(raw)
    assert req.systemInstruction is not None
    assert isinstance(req.systemInstruction.parts[0], GeminiTextPart)
    assert req.systemInstruction.parts[0].text.startswith("You are terse")
    assert [c.role for c in req.contents] == ["user", "model", "user"]


def test_gemini_request_round_trip_function_call_round_trip() -> None:
    raw = {
        "contents": [
            {"parts": [{"text": "What is the weather in Paris?"}], "role": "user"},
            {
                "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}],
                "role": "model",
            },
            {
                "parts": [
                    {
                        "functionResponse": {
                            "name": "get_weather",
                            "response": {"weather": "sunny, 22C"},
                        }
                    }
                ],
                "role": "user",
            },
        ],
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": "get_weather",
                        "description": "Get current weather in a city",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {"city": {"type": "STRING"}},
                            "required": ["city"],
                        },
                    }
                ]
            }
        ],
    }
    req = GeminiRequestBody.model_validate(raw)
    # Content parts dispatch by key.
    assert isinstance(req.contents[1].parts[0], GeminiFunctionCallPart)
    assert req.contents[1].parts[0].functionCall.name == "get_weather"
    assert req.contents[1].parts[0].functionCall.args == {"city": "Paris"}
    assert isinstance(req.contents[2].parts[0], GeminiFunctionResponsePart)
    assert req.contents[2].parts[0].functionResponse.name == "get_weather"
    assert req.contents[2].parts[0].functionResponse.response == {"weather": "sunny, 22C"}
    # Tool declarations parse.
    assert len(req.tools) == 1
    assert req.tools[0].functionDeclarations[0].name == "get_weather"


# --------------------------------------------------------------------------
# Part-variant dispatch — synthetic
# --------------------------------------------------------------------------


def test_gemini_inline_data_part_dispatches() -> None:
    content = GeminiContent.model_validate(
        {
            "role": "user",
            "parts": [{"inlineData": {"mimeType": "image/png", "data": "aGVsbG8="}}],
        }
    )
    assert isinstance(content.parts[0], GeminiInlineDataPart)
    assert content.parts[0].inlineData.mimeType == "image/png"
    assert content.parts[0].inlineData.data == "aGVsbG8="


def test_gemini_file_data_part_dispatches() -> None:
    content = GeminiContent.model_validate(
        {
            "role": "user",
            "parts": [
                {
                    "fileData": {
                        "mimeType": "application/pdf",
                        "fileUri": "gs://my-bucket/notes.pdf",
                    }
                }
            ],
        }
    )
    assert isinstance(content.parts[0], GeminiFileDataPart)
    assert content.parts[0].fileData.fileUri == "gs://my-bucket/notes.pdf"


def test_gemini_executable_code_part_dispatches() -> None:
    content = GeminiContent.model_validate(
        {
            "role": "model",
            "parts": [
                {"executableCode": {"language": "PYTHON", "code": "print(2+2)"}},
                {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "4\n"}},
            ],
        }
    )
    assert isinstance(content.parts[0], GeminiExecutableCodePart)
    assert isinstance(content.parts[1], GeminiCodeExecutionResultPart)


def test_gemini_thought_signature_part_dispatches() -> None:
    content = GeminiContent.model_validate(
        {"role": "model", "parts": [{"thoughtSignature": "opaque-state"}]}
    )
    assert isinstance(content.parts[0], GeminiThoughtSignaturePart)


def test_gemini_unknown_part_catches_unmodelled_key() -> None:
    """Unknown top-level key on a part falls through to GeminiUnknownPart."""
    content = GeminiContent.model_validate(
        {"role": "user", "parts": [{"somethingBrandNew": {"foo": "bar"}}]}
    )
    assert isinstance(content.parts[0], GeminiUnknownPart)


# --------------------------------------------------------------------------
# Response-side — real POC row + synthetic usageMetadata edges
# --------------------------------------------------------------------------


def test_gemini_response_round_trip_full() -> None:
    raw = {
        "candidates": [
            {
                "content": {"parts": [{"text": "ACK-A"}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "createTime": "2026-09-05T02:43:59.129977Z",
        "modelVersion": "gemini-2.5-flash",
        "responseId": "74Gbarn3B8at1t8PvomZiA4",
        "usageMetadata": {
            "candidatesTokenCount": 3,
            "candidatesTokensDetails": [{"modality": "TEXT", "tokenCount": 3}],
            "promptTokenCount": 8,
            "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 8}],
            "thoughtsTokenCount": 20,
            "totalTokenCount": 31,
            "trafficType": "ON_DEMAND",
        },
    }
    resp = GeminiResponse.model_validate(raw)
    assert len(resp.candidates) == 1
    cand = resp.candidates[0]
    assert isinstance(cand, GeminiCandidate)
    assert cand.finishReason == "STOP"
    assert cand.content is not None
    assert cand.content.role == "model"
    assert isinstance(cand.content.parts[0], GeminiTextPart)
    assert resp.usageMetadata.promptTokenCount == 8
    assert resp.usageMetadata.candidatesTokenCount == 3
    assert resp.usageMetadata.thoughtsTokenCount == 20


def test_gemini_response_usage_metadata_defaults() -> None:
    """Absent usageMetadata fields default to zero — envelope reads them
    without ``or 0`` guards."""
    usage = GeminiUsageMetadata.model_validate({})
    assert usage.promptTokenCount == 0
    assert usage.candidatesTokenCount == 0
    assert usage.thoughtsTokenCount == 0
    assert usage.cachedContentTokenCount == 0


def test_gemini_response_absent_candidates_is_empty_list() -> None:
    """SAFETY finish with no candidates is still a valid response shape."""
    resp = GeminiResponse.model_validate({"candidates": []})
    assert resp.candidates == []


# --------------------------------------------------------------------------
# Strict-part discriminator behaviour
# --------------------------------------------------------------------------


def test_gemini_multi_key_part_falls_through_to_unknown() -> None:
    """Every modelled part variant is _StrictModel — a pathological dict
    carrying multiple top-level keys satisfies none of them (each strict
    variant sees an extra it forbids) and falls to GeminiUnknownPart.
    Documents the smart-union behaviour on malformed input; Gemini
    never emits multi-key parts in practice.
    """
    content = GeminiContent.model_validate(
        {
            "role": "model",
            "parts": [{"text": "ignore", "functionCall": {"name": "x", "args": {}}}],
        }
    )
    assert isinstance(content.parts[0], GeminiUnknownPart)


def test_gemini_strict_part_missing_key_rejects() -> None:
    """A part with NEITHER text nor any modelled key falls to unknown."""
    content = GeminiContent.model_validate({"role": "user", "parts": [{}]})
    assert isinstance(content.parts[0], GeminiUnknownPart)


def test_gemini_system_instruction_empty_parts_defaults_ok() -> None:
    sysinst = GeminiSystemInstruction.model_validate({})
    assert sysinst.parts == []


def test_gemini_bad_role_rejects() -> None:
    """Content.role is Literal["user", "model"] — other values fail."""
    with pytest.raises(ValidationError):
        GeminiContent.model_validate({"role": "assistant", "parts": []})
