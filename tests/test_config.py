"""Config loading via pydantic-settings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from slashid_bedrock_forwarder.config import Config, load_config


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    load_config.cache_clear()


def test_config_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.slashid.com/")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "tok")

    cfg = load_config()
    assert isinstance(cfg, Config)
    assert cfg.endpoint == "https://api.slashid.com/"
    assert cfg.push_token == "tok"
    assert cfg.identity_source_type == "manual_import"  # default
    assert cfg.request_timeout_seconds == 10.0  # default
    assert cfg.max_retries == 3  # default


def test_config_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.slashid.com")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "tok")
    monkeypatch.setenv("SLASHID_IDENTITY_SOURCE_TYPE", "aws_account")
    monkeypatch.setenv("SLASHID_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("SLASHID_MAX_RETRIES", "5")

    cfg = load_config()
    assert cfg.identity_source_type == "aws_account"
    assert cfg.request_timeout_seconds == 30.0
    assert cfg.max_retries == 5


def test_config_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SLASHID_ENDPOINT", "SLASHID_PUSH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError):
        load_config()
