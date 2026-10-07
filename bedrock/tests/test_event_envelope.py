"""Tests for ``bedrock_envelope`` — Bedrock MIL record → EventEnvelope.

The Bedrock half of the event build: identity extraction, timestamp
normalisation, stop-reason coercion, top-level token counts, and the
model-catalog lookup. Pure ``build_event_from_normalized`` behaviour
that consumes the envelope is tested in shared/tests/test_events.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AWSIdentityDetails, build_event_from_normalized
from slashid_ai_forwarder_core.normalize.converse.normalize import (
    converse_dict_to_normalized,
)

from slashid_bedrock_forwarder.event_envelope import bedrock_envelope
from slashid_bedrock_forwarder.mil_normalize import normalize_record


def _config(*, include_raw_content: bool = False, max_content_size: int = 100_000) -> BaseConfig:
    return BaseConfig(
        endpoint="http://test",
        push_token="test",
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )


def _mil_record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "requestId": "req-1",
        "timestamp": "2026-06-01T12:00:00Z",
        "modelId": "us.anthropic.claude-sonnet-4-6",
        "accountId": "123456789012",
        "identity": {
            "arn": "arn:aws:iam::123456789012:user/alice",
            "accessKeyId": "AKIAEXAMPLE",
        },
        "input": {"inputTokenCount": 100, "cacheReadInputTokenCount": 5},
        "output": {"outputTokenCount": 50, "outputBodyJson": {"stopReason": "end_turn"}},
    }
    base.update(overrides)
    return base


# --- drop conditions -------------------------------------------------------


def test_bedrock_envelope_returns_none_without_request_id() -> None:
    """Records missing ``requestId`` (body-offload S3 pseudo-records) drop."""
    record = _mil_record()
    del record["requestId"]
    assert bedrock_envelope(record) is None


def test_bedrock_envelope_returns_none_without_identity() -> None:
    """Regression for R1: a record with no usable principal ARN should drop,
    not ship as ``identity_details.principal_arn = ""``."""
    record = _mil_record()
    record["identity"] = {}  # no arn, no resolved_arn
    assert bedrock_envelope(record) is None


def test_bedrock_envelope_returns_none_with_no_identity_block() -> None:
    record = _mil_record()
    del record["identity"]
    assert bedrock_envelope(record) is None


# --- identity extraction ---------------------------------------------------


def test_bedrock_envelope_omits_access_key_when_missing() -> None:
    record = _mil_record(identity={"arn": "arn:aws:iam::123:user/bob"})
    env = bedrock_envelope(record)
    assert env is not None
    assert isinstance(env.identity_details, AWSIdentityDetails)
    assert env.identity_details.principal_arn == "arn:aws:iam::123:user/bob"
    assert env.identity_details.access_key_id is None


def test_bedrock_envelope_prefers_resolved_arn_over_arn() -> None:
    """When both keys are present, ``resolved_arn`` wins — MIL's own
    role-chain-unroll hint takes precedence over the raw assumed-role ARN."""
    record = _mil_record(
        identity={
            "arn": "arn:aws:sts::123:assumed-role/Role/session",
            "resolved_arn": "arn:aws:iam::123:user/real-user",
            "accessKeyId": "AKIA...",
        }
    )
    env = bedrock_envelope(record)
    assert env is not None
    assert isinstance(env.identity_details, AWSIdentityDetails)
    assert env.identity_details.principal_arn == "arn:aws:iam::123:user/real-user"


# --- model extraction ------------------------------------------------------


def test_bedrock_envelope_populates_raw_model_id() -> None:
    # No region in base record → no catalog lookup → id falls back to raw
    record = _mil_record()
    env = bedrock_envelope(record)
    assert env is not None
    assert env.model.id == "us.anthropic.claude-sonnet-4-6"
    assert env.model.raw_model_id == "us.anthropic.claude-sonnet-4-6"
    assert env.model.name is None
    assert env.model.provider is None


def test_bedrock_envelope_uses_arn_as_id_when_raw_is_arn() -> None:
    arn = "arn:aws:bedrock:us-east-2:851725497009:inference-profile/us.anthropic.claude-sonnet-4-6"
    record = _mil_record(modelId=arn, region="us-east-2")
    env = bedrock_envelope(record)
    assert env is not None
    # Raw is already an ARN → used directly, no catalog needed
    assert env.model.id == arn
    assert env.model.raw_model_id == arn


def test_bedrock_envelope_enriches_model_from_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    from slashid_ai_forwarder_core import model_catalog

    monkeypatch.setattr(
        model_catalog,
        "_catalogs",
        {
            "us-east-2": {
                "anthropic.claude-sonnet-4-6": model_catalog.ModelInfo(
                    arn="arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6",
                    name="Claude Sonnet 4.6",
                    provider="Anthropic",
                )
            }
        },
    )
    record = _mil_record(modelId="us.anthropic.claude-sonnet-4-6", region="us-east-2")
    env = bedrock_envelope(record)
    assert env is not None
    assert env.model.id == "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-sonnet-4-6"
    assert env.model.name == "Claude Sonnet 4.6"
    assert env.model.provider == "Anthropic"
    assert env.model.raw_model_id == "us.anthropic.claude-sonnet-4-6"


def test_bedrock_envelope_model_region_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``model_region`` overrides ``record["region"]`` for the catalog lookup."""
    from slashid_ai_forwarder_core import model_catalog

    monkeypatch.setattr(
        model_catalog,
        "_catalogs",
        {
            "eu-west-1": {
                "anthropic.claude-sonnet-4-6": model_catalog.ModelInfo(
                    arn="arn:aws:bedrock:eu-west-1::foundation-model/anthropic.claude-sonnet-4-6",
                    name="Claude Sonnet 4.6",
                    provider="Anthropic",
                )
            }
        },
    )
    # Record's region is different — but model_region wins.
    record = _mil_record(modelId="anthropic.claude-sonnet-4-6", region="us-east-2")
    env = bedrock_envelope(record, model_region="eu-west-1")
    assert env is not None
    assert env.model.id.startswith("arn:aws:bedrock:eu-west-1::")


# --- tokens ----------------------------------------------------------------


def test_bedrock_envelope_extracts_tokens_from_top_level_fields() -> None:
    record = _mil_record(
        input={
            "inputTokenCount": 42,
            "cacheReadInputTokenCount": 3,
            "cacheWriteInputTokenCount": 7,
        },
        output={"outputTokenCount": 11, "outputBodyJson": {"stopReason": "end_turn"}},
    )
    env = bedrock_envelope(record)
    assert env is not None
    assert env.tokens.input == 42
    assert env.tokens.output == 11
    assert env.tokens.cache_read == 3
    assert env.tokens.cache_write == 7
    assert env.tokens.reasoning == 0


def test_bedrock_envelope_reads_reasoning_token_count() -> None:
    record = _mil_record(output={"outputTokenCount": 11, "reasoningTokenCount": 12})
    env = bedrock_envelope(record)
    assert env is not None
    assert env.tokens.reasoning == 12


async def test_bedrock_envelope_openai_responses_tokens() -> None:
    path = Path(__file__).parent / "fixtures" / "openai_responses_mil.json"
    record: dict[str, Any] = json.loads(path.read_text())
    await normalize_record(record, config=_config())
    env = bedrock_envelope(record)
    assert env is not None
    assert env.parsed_as == "openai-responses"
    assert (env.tokens.input, env.tokens.output, env.tokens.reasoning) == (12, 11, 12)


def test_bedrock_envelope_defaults_tokens_to_zero_when_absent() -> None:
    record = _mil_record(input={}, output={})
    env = bedrock_envelope(record)
    assert env is not None
    assert env.tokens.input == 0
    assert env.tokens.output == 0
    assert env.tokens.cache_read == 0
    assert env.tokens.cache_write == 0


# --- timestamp normalisation ----------------------------------------------


def test_bedrock_envelope_normalises_zulu_timestamp() -> None:
    """MIL emits `...Z`; ISO 8601 offset form is what the wire wants."""
    record = _mil_record(timestamp="2026-06-01T12:00:00Z")
    env = bedrock_envelope(record)
    assert env is not None
    assert env.timestamp == "2026-06-01T12:00:00+00:00"


def test_bedrock_envelope_falls_back_to_event_time() -> None:
    record = _mil_record()
    del record["timestamp"]
    record["eventTime"] = "2026-06-01T12:34:56Z"
    env = bedrock_envelope(record)
    assert env is not None
    assert env.timestamp == "2026-06-01T12:34:56+00:00"


def test_bedrock_envelope_fills_now_when_timestamps_absent() -> None:
    record = _mil_record()
    del record["timestamp"]
    env = bedrock_envelope(record)
    assert env is not None
    # datetime.now(tz=UTC).isoformat() format sanity check — the value
    # itself is a moving target.
    assert env.timestamp.endswith("+00:00") or "T" in env.timestamp


# --- parsed_as -------------------------------------------------------------


async def test_build_event_populates_parsed_as_from_envelope() -> None:
    """normalize_record sets record["_parsed_as"]; the envelope surfaces it."""
    record = _mil_record()
    record["_parsed_as"] = "anthropic-message"
    env = bedrock_envelope(record)
    assert env is not None
    assert env.parsed_as == "anthropic-message"
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        env,
        config=_config(),
    )
    assert event.parsed_as == "anthropic-message"


async def test_build_event_parsed_as_defaults_to_unknown() -> None:
    """If _parsed_as is missing (bypass path — defensive; not exercised in
    normal handler flow), the envelope falls back to "unknown"."""
    record = _mil_record()  # no _parsed_as set
    env = bedrock_envelope(record)
    assert env is not None
    assert env.parsed_as == "unknown"
    event = await build_event_from_normalized(
        await converse_dict_to_normalized(record, config=_config()),
        env,
        config=_config(),
    )
    assert event.parsed_as == "unknown"


@pytest.mark.parametrize(
    ("fixture", "parsed_as", "tokens"),
    [
        ("openai_chat_trivial_mil.json", "openai-chat", (13, 11, 19)),
        ("openai_chat_large_prompt_repeat_cached_mil.json", "openai-chat", (2, 5, 0)),
    ],
)
async def test_bedrock_envelope_openai_chat_tokens(
    fixture: str, parsed_as: str, tokens: tuple[int, int, int]
) -> None:
    record: dict[str, Any] = json.loads((Path(__file__).parent / "fixtures" / fixture).read_text())
    await normalize_record(record, config=_config())
    env = bedrock_envelope(record)
    assert env is not None
    assert env.parsed_as == parsed_as
    assert (env.tokens.input, env.tokens.output, env.tokens.reasoning) == tokens


async def test_bedrock_envelope_openai_chat_rejected_request() -> None:
    path = Path(__file__).parent / "fixtures" / "openai_chat_rejected_mil.json"
    record: dict[str, Any] = json.loads(path.read_text())
    await normalize_record(record, config=_config())
    env = bedrock_envelope(record)
    assert env is not None
    assert env.parsed_as == "unknown"


@pytest.mark.parametrize(
    "fixture", ["invoke_llama_native_mil.json", "invoke_llama_native_stream_mil.json"]
)
async def test_bedrock_envelope_llama_tokens(fixture: str) -> None:
    record: dict[str, Any] = json.loads((Path(__file__).parent / "fixtures" / fixture).read_text())
    await normalize_record(record, config=_config())
    env = bedrock_envelope(record)
    assert env is not None
    assert env.parsed_as.startswith("llama")
    assert (env.tokens.input, env.tokens.output) == (17, 5)
