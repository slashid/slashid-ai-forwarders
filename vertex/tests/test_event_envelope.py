"""Tests for ``vertex_envelope`` — BQ Entry → EventEnvelope."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
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
    request_body: GeminiRequestBody | None = None,
    response_body: GeminiResponse | None = None,
    api_method: str = "GenerateContent",
) -> Entry:
    return Entry(
        request_id=request_id,
        logging_time=logging_time or datetime(2026, 9, 5, 2, 43, 59, tzinfo=UTC),
        model_path=model_path,
        region=region,
        request_body=request_body
        or GeminiRequestBody.model_validate(
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
        api_method=api_method,
    )


def test_envelope_populates_basic_fields() -> None:
    env = vertex_envelope(_entry())
    assert env is not None
    assert env.request_id == "3292372995731278848"
    assert env.timestamp == "2026-09-05T02:43:59+00:00"
    assert env.parsed_as == PARSED_AS
    assert env.parsed_as == "vertex-gemini-generate"


def test_envelope_identity_is_empty_gcp() -> None:
    """V1 punts on identity — GCPIdentityDetails() with credential_chain=None."""
    env = vertex_envelope(_entry())
    assert env is not None
    assert isinstance(env.identity_details, GCPIdentityDetails)
    assert env.identity_details.credential_chain is None
    # Serialize form is the minimal wire shape.
    wire = env.identity_details.model_dump(mode="json", exclude_none=True)
    assert wire == {"kind": "gcp"}


# Representative model paths from every Vertex Model Garden publisher we
# expect to encounter. Phase 3.1 only ships Gemini generateContent (the
# ``google`` rows), but the parser is exercised across every publisher
# now so the wire ``provider`` field stays honest when phase 3.3+ adds
# rawPredict paths for anthropic / meta / mistralai / ai21.
@pytest.mark.parametrize(
    "model_path,expected_provider",
    [
        # Google — Gemini text, embeddings, image
        ("publishers/google/models/gemini-2.5-flash", "google"),
        ("publishers/google/models/gemini-2.5-pro", "google"),
        ("publishers/google/models/gemini-2.0-flash-001", "google"),
        ("publishers/google/models/text-embedding-005", "google"),
        # Anthropic — Claude on Vertex uses @version suffix
        ("publishers/anthropic/models/claude-3-5-sonnet-v2@20241022", "anthropic"),
        ("publishers/anthropic/models/claude-3-5-haiku@20241022", "anthropic"),
        ("publishers/anthropic/models/claude-opus-4@20250514", "anthropic"),
        ("publishers/anthropic/models/claude-sonnet-4@20250514", "anthropic"),
        # Meta — Llama on Vertex uses ``-maas`` (Model-as-a-Service) suffix
        ("publishers/meta/models/llama-3.3-70b-instruct-maas", "meta"),
        ("publishers/meta/models/llama-3.1-405b-instruct-maas", "meta"),
        # Mistral
        ("publishers/mistralai/models/mistral-large-2411", "mistralai"),
        ("publishers/mistralai/models/mistral-nemo", "mistralai"),
        ("publishers/mistralai/models/codestral-2501", "mistralai"),
        # AI21
        ("publishers/ai21/models/jamba-1.5-large", "ai21"),
    ],
)
def test_envelope_provider_parsed_from_publisher_segment(
    model_path: str, expected_provider: str
) -> None:
    env = vertex_envelope(_entry(model_path=model_path))
    assert env is not None
    assert env.model.provider == expected_provider
    # ID and raw_model_id always echo the full publisher path unchanged.
    assert env.model.id == model_path
    assert env.model.raw_model_id == model_path


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


def test_envelope_drops_when_request_id_empty() -> None:
    """Defensive drop path — BQ shouldn't emit empty request_id, but
    match bedrock_envelope's shape."""
    env = vertex_envelope(_entry(request_id=""))
    assert env is None


def test_envelope_reads_pre_attached_identity_details() -> None:
    """When Entry.identity_details is populated by the source, envelope
    passes it through unchanged (does not construct a fresh empty one)."""
    from datetime import UTC, datetime

    from slashid_ai_forwarder_core.events import GCPCredential, GCPIdentityDetails
    from slashid_ai_forwarder_core.normalize.gemini.schema import (
        GeminiRequestBody,
        GeminiResponse,
    )

    from slashid_vertex_forwarder.event_envelope import vertex_envelope
    from slashid_vertex_forwarder.event_source import Entry

    entry = Entry(
        request_id="1",
        logging_time=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        model_path="publishers/google/models/gemini-2.5-flash",
        region="us-central1",
        request_body=GeminiRequestBody.model_validate({"contents": []}),
        response_body=GeminiResponse.model_validate({"candidates": [], "usageMetadata": {}}),
        api_method="GenerateContent",
        request_latency_ms=100.0,
        identity_details=GCPIdentityDetails(
            credential_chain=[GCPCredential(principal_email="alice@example.com")]
        ),
    )
    env = vertex_envelope(entry)
    assert env is not None
    assert isinstance(env.identity_details, GCPIdentityDetails)
    assert env.identity_details.credential_chain is not None
    assert env.identity_details.credential_chain[0].principal_email == "alice@example.com"


@pytest.mark.parametrize(
    "path, expected",
    [
        (
            "publishers/anthropic/models/claude-sonnet-4-5",
            ("anthropic", "claude-sonnet-4-5"),
        ),
        (
            "projects/p/locations/r/publishers/anthropic/models/claude-sonnet-4-5",
            ("anthropic", "claude-sonnet-4-5"),
        ),
        (
            "publishers/google/models/my-tuned/endpoints/abc123",
            ("google", "my-tuned/endpoints/abc123"),
        ),
        ("not-a-path", (None, None)),
        ("", (None, None)),
    ],
)
def test_parse_model_path(path: str, expected: tuple[str | None, str | None]) -> None:
    from slashid_vertex_forwarder.event_envelope import _parse_model_path

    assert _parse_model_path(path) == expected


def test_vertex_envelope_populates_model_name() -> None:
    """BQ envelope now emits AIModel.name from the model segment of the
    publisher path — consistency with the upcoming audit-only envelope."""
    env = vertex_envelope(_entry(model_path="publishers/google/models/gemini-2.5-flash"))
    assert env is not None
    assert env.model.name == "gemini-2.5-flash"
    assert env.model.provider == "google"
    assert env.model.id == "publishers/google/models/gemini-2.5-flash"
    assert env.model.raw_model_id == "publishers/google/models/gemini-2.5-flash"


def test_envelope_empty_identity_still_serializes_to_kind_gcp() -> None:
    from datetime import UTC, datetime

    from slashid_ai_forwarder_core.events import GCPIdentityDetails
    from slashid_ai_forwarder_core.normalize.gemini.schema import (
        GeminiRequestBody,
        GeminiResponse,
    )

    from slashid_vertex_forwarder.event_envelope import vertex_envelope
    from slashid_vertex_forwarder.event_source import Entry

    entry = Entry(
        request_id="1",
        logging_time=datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC),
        model_path="publishers/google/models/gemini-2.5-flash",
        region="us-central1",
        request_body=GeminiRequestBody.model_validate({"contents": []}),
        response_body=GeminiResponse.model_validate({"candidates": [], "usageMetadata": {}}),
        api_method="GenerateContent",
    )
    env = vertex_envelope(entry)
    assert env is not None
    assert isinstance(env.identity_details, GCPIdentityDetails)
    dumped = env.identity_details.model_dump(mode="json", exclude_none=True)
    assert dumped == {"kind": "gcp"}
