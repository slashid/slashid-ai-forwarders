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
    # Trailing slash from the env var gets normalised away.
    assert cfg.endpoint == "https://api.slashid.com"
    assert cfg.push_token == "tok"
    # Privacy defaults: opted out, no raw prompt/response text on the wire.
    assert cfg.include_raw_content is False
    assert cfg.request_timeout_seconds == 10.0
    assert cfg.max_retries == 3


def test_config_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.slashid.com")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "tok")
    monkeypatch.setenv("SLASHID_INCLUDE_RAW_CONTENT", "true")
    monkeypatch.setenv("SLASHID_REQUEST_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("SLASHID_MAX_RETRIES", "5")

    cfg = load_config()
    assert cfg.include_raw_content is True
    assert cfg.request_timeout_seconds == 30.0
    assert cfg.max_retries == 5


def test_config_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SLASHID_ENDPOINT", "SLASHID_PUSH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError):
        load_config()


def test_config_strips_trailing_slash_from_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for B2: customer pasting `https://api.slashid.com/` should
    not produce `https://api.slashid.com//ip/nhi/events/ai-invocations` later."""
    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.slashid.com/")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "tok")

    cfg = load_config()
    assert cfg.endpoint == "https://api.slashid.com"
