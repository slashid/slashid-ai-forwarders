from __future__ import annotations

import pytest
from pydantic import ValidationError

from slashid_ai_forwarder_core.config_base import BaseConfig


def _config() -> BaseConfig:
    return BaseConfig(endpoint="http://test", push_token="t")


def test_endpoint_defaults_to_the_production_api() -> None:
    assert BaseConfig(push_token="t").endpoint == "https://api.slashid.com"


def test_endpoint_can_be_overridden_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.eu.slashid.example/")
    assert BaseConfig(push_token="t").endpoint == "https://api.eu.slashid.example"


def test_input_scope_defaults_to_round_and_depth_to_ten() -> None:
    config = _config()
    assert config.input_scope == "round"
    assert config.round_link_depth == 10


def test_input_scope_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_INPUT_SCOPE", "session")
    monkeypatch.setenv("SLASHID_ROUND_LINK_DEPTH", "4")
    config = _config()
    assert (config.input_scope, config.round_link_depth) == ("session", 4)


def test_input_scope_rejects_unknown_values_and_zero_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLASHID_INPUT_SCOPE", "turn")
    with pytest.raises(ValidationError):
        _config()
    monkeypatch.setenv("SLASHID_INPUT_SCOPE", "round")
    monkeypatch.setenv("SLASHID_ROUND_LINK_DEPTH", "0")
    with pytest.raises(ValidationError):
        _config()
