"""Tests for the Bedrock foundation-model catalog."""

from __future__ import annotations

import pytest

from slashid_bedrock_forwarder.model_catalog import (
    ModelInfo,
    _canonical_id,
    get_model_info,
    reset_catalog,
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("slashid_bedrock_forwarder.model_catalog._catalogs", {})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("amazon.nova-micro-v1:0", "amazon.nova-micro-v1:0"),
        ("us.amazon.nova-micro-v1:0", "amazon.nova-micro-v1:0"),
        ("eu.amazon.nova-micro-v1:0", "amazon.nova-micro-v1:0"),
        ("apac.amazon.nova-micro-v1:0", "amazon.nova-micro-v1:0"),
        (
            "arn:aws:bedrock:us-east-2:851725497009:inference-profile/us.amazon.nova-micro-v1:0",
            "amazon.nova-micro-v1:0",
        ),
        (
            "arn:aws:bedrock:ap-northeast-1:851725497009:inference-profile/apac.amazon.nova-micro-v1:0",
            "amazon.nova-micro-v1:0",
        ),
    ],
)
def test_canonical_id(raw: str, expected: str) -> None:
    assert _canonical_id(raw) == expected


def test_get_model_info_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    info = ModelInfo(
        arn="arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-micro-v1:0",
        name="Nova Micro",
        provider="Amazon",
    )
    monkeypatch.setattr(
        "slashid_bedrock_forwarder.model_catalog._catalogs",
        {"us-east-2": {"amazon.nova-micro-v1:0": info}},
    )
    # canonical key → found via stripped fallback
    assert get_model_info("us.amazon.nova-micro-v1:0", "us-east-2") == info
    assert get_model_info("amazon.nova-micro-v1:0", "us-east-2") == info


def test_get_model_info_raw_key_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the catalog has the raw key, it wins over the stripped canonical."""
    info_raw = ModelInfo(
        arn="arn:aws:bedrock:us-east-2::foundation-model/us.amazon.nova-micro-v1:0",
        name="Nova Micro (raw key)",
        provider="Amazon",
    )
    info_canonical = ModelInfo(
        arn="arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-micro-v1:0",
        name="Nova Micro (canonical key)",
        provider="Amazon",
    )
    monkeypatch.setattr(
        "slashid_bedrock_forwarder.model_catalog._catalogs",
        {
            "us-east-2": {
                "us.amazon.nova-micro-v1:0": info_raw,
                "amazon.nova-micro-v1:0": info_canonical,
            }
        },
    )
    assert get_model_info("us.amazon.nova-micro-v1:0", "us-east-2") == info_raw


def test_get_model_info_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "slashid_bedrock_forwarder.model_catalog._catalogs",
        {"us-east-2": {}},
    )
    assert get_model_info("unknown.model-v1:0", "us-east-2") is None


def test_get_model_info_arn_input(monkeypatch: pytest.MonkeyPatch) -> None:
    info = ModelInfo(
        arn="arn:aws:bedrock:us-east-2::foundation-model/amazon.nova-micro-v1:0",
        name="Nova Micro",
        provider="Amazon",
    )
    monkeypatch.setattr(
        "slashid_bedrock_forwarder.model_catalog._catalogs",
        {"us-east-2": {"amazon.nova-micro-v1:0": info}},
    )
    arn_input = "arn:aws:bedrock:us-east-2:851725497009:inference-profile/us.amazon.nova-micro-v1:0"
    assert get_model_info(arn_input, "us-east-2") == info


def test_catalog_loaded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    load_calls: list[str] = []

    def fake_load(region: str) -> dict[str, ModelInfo]:
        load_calls.append(region)
        return {}

    monkeypatch.setattr("slashid_bedrock_forwarder.model_catalog._load", fake_load)
    get_model_info("amazon.nova-micro-v1:0", "us-east-2")
    get_model_info("amazon.nova-micro-v1:0", "us-east-2")
    assert load_calls == ["us-east-2"]


def test_reset_catalog_clears_state(monkeypatch: pytest.MonkeyPatch) -> None:
    load_calls: list[str] = []

    def fake_load(region: str) -> dict[str, ModelInfo]:
        load_calls.append(region)
        return {}

    monkeypatch.setattr("slashid_bedrock_forwarder.model_catalog._load", fake_load)
    get_model_info("amazon.nova-micro-v1:0", "us-east-2")
    reset_catalog()
    get_model_info("amazon.nova-micro-v1:0", "us-east-2")
    assert load_calls == ["us-east-2", "us-east-2"]
