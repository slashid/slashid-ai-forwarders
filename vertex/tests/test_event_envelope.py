"""Tests for ``vertex_envelope`` — BQ Entry → EventEnvelope."""

from __future__ import annotations

from datetime import UTC, datetime

from slashid_ai_forwarder_core.events import GCPIdentityDetails
from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)

from slashid_vertex_forwarder.event_envelope import PARSED_AS, vertex_envelope
from slashid_vertex_forwarder.event_source import Entry


def _entry(
    *,
    request_id: str = "3292372995731278848",
    logging_time: datetime | None = None,
    model_path: str = "publishers/google/models/gemini-2.5-flash",
    region: str = "us-central1",
    response_body: GeminiResponse | None = None,
) -> Entry:
    return Entry(
        request_id=request_id,
        logging_time=logging_time or datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC),
        model_path=model_path,
        region=region,
        request_body=GeminiRequestBody.model_validate(
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}
        ),
        response_body=response_body
        or GeminiResponse.model_validate(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "ok"}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 3,
                    "thoughtsTokenCount": 20,
                    "totalTokenCount": 31,
                },
            }
        ),
    )


def test_envelope_populates_basic_fields() -> None:
    env = vertex_envelope(_entry())
    assert env is not None
    assert env.request_id == "3292372995731278848"
    assert env.timestamp == "2026-09-05T02:43:59+00:00"
    assert env.parsed_as == PARSED_AS
    assert env.parsed_as == "vertex-gemini-generate"
    assert env.stop_reason == "end_turn"


def test_envelope_identity_is_empty_gcp() -> None:
    """V1 punts on identity — GCPIdentityDetails() with all fields None."""
    env = vertex_envelope(_entry())
    assert env is not None
    assert isinstance(env.identity_details, GCPIdentityDetails)
    assert env.identity_details.principal_email is None
    assert env.identity_details.service_account_email is None
    assert env.identity_details.oauth_client_id is None
    # Serialize form is the minimal wire shape.
    wire = env.identity_details.model_dump(mode="json", exclude_none=True)
    assert wire == {"kind": "gcp"}


def test_envelope_model_full_publisher_path() -> None:
    env = vertex_envelope(_entry(model_path="publishers/google/models/gemini-2.5-pro"))
    assert env is not None
    assert env.model.id == "publishers/google/models/gemini-2.5-pro"
    assert env.model.raw_model_id == "publishers/google/models/gemini-2.5-pro"
    assert env.model.provider == "google"


def test_envelope_provider_parsed_from_non_google_publisher() -> None:
    """Vertex Model Garden hosts anthropic/meta/mistralai via rawPredict —
    the publisher segment of the model path is the source of truth. Phase
    3.1 only surfaces google publishers, but the parse stays honest for
    the multi-publisher path coming in phase 3.3+."""
    env = vertex_envelope(
        _entry(model_path="publishers/anthropic/models/claude-3-5-sonnet-v2@20241022")
    )
    assert env is not None
    assert env.model.provider == "anthropic"


def test_envelope_provider_none_when_model_path_unfamiliar() -> None:
    """Malformed / unexpected model_path shapes don't crash — provider
    falls to None and the wire event still ships with the raw id."""
    env = vertex_envelope(_entry(model_path="not-a-publisher-path"))
    assert env is not None
    assert env.model.provider is None
    assert env.model.id == "not-a-publisher-path"


def test_envelope_tokens_from_usage_metadata() -> None:
    env = vertex_envelope(_entry())
    assert env is not None
    assert env.tokens.input == 8
    assert env.tokens.output == 3
    assert env.tokens.reasoning == 20
    assert env.tokens.cache_read == 0
    # Gemini exposes no cache-write counter; stays zero.
    assert env.tokens.cache_write == 0


def test_envelope_tokens_with_cached_content() -> None:
    resp = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "ok"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 20,
                "cachedContentTokenCount": 60,
            },
        }
    )
    env = vertex_envelope(_entry(response_body=resp))
    assert env is not None
    assert env.tokens.cache_read == 60


def test_envelope_safety_stop_reason_maps_to_content_filtered() -> None:
    resp = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "partial"}]},
                    "finishReason": "SAFETY",
                }
            ],
        }
    )
    env = vertex_envelope(_entry(response_body=resp))
    assert env is not None
    assert env.stop_reason == "content_filtered"


def test_envelope_no_candidates_stop_reason_is_none() -> None:
    """SAFETY block that suppresses candidates entirely: no finishReason
    to derive from → stop_reason stays None on the envelope."""
    resp = GeminiResponse.model_validate({"candidates": [], "usageMetadata": {}})
    env = vertex_envelope(_entry(response_body=resp))
    assert env is not None
    assert env.stop_reason is None


def test_envelope_drops_when_request_id_empty() -> None:
    """Defensive drop path — BQ shouldn't emit empty request_id, but
    match bedrock_envelope's shape."""
    env = vertex_envelope(_entry(request_id=""))
    assert env is None


def test_envelope_unknown_finish_reason_folds_to_unknown() -> None:
    resp = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "x"}]},
                    "finishReason": "SOME_NEW_ENUM_VALUE",
                }
            ],
        }
    )
    env = vertex_envelope(_entry(response_body=resp))
    assert env is not None
    assert env.stop_reason == "unknown"
