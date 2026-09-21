"""Config env-var parsing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from slashid_anthropic_forwarder.config import Config


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "SLASHID_ENDPOINT": "https://api.slashid.com",
        "SLASHID_PUSH_TOKEN": "token",
        "SLASHID_HOOK_SIGNING_SECRET": "whsec_AAA",
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_signing_secrets_split_on_comma(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_HOOK_SIGNING_SECRET="whsec_AAA, whsec_BBB")
    assert Config().signing_secrets == ["whsec_AAA", "whsec_BBB"]


def test_defaults_are_observe_only_and_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.shadow_mode is True
    assert cfg.verdict_fail_mode == "allow"
    assert cfg.policy_url is None
    assert cfg.preflight_enabled is True
    assert cfg.hook_allow_unsigned is False
    assert cfg.capture_bucket is None


def test_fail_mode_must_be_allow_or_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_VERDICT_FAIL_MODE="maybe")
    with pytest.raises(ValidationError):
        Config()


def test_signing_secret_required_unless_unsigned_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_HOOK_SIGNING_SECRET")
    with pytest.raises(ValidationError):
        Config()
    monkeypatch.setenv("SLASHID_HOOK_ALLOW_UNSIGNED", "true")
    assert Config().signing_secrets == []


def test_unsigned_with_policy_url_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # The policy receiver answers 401 to an unsigned forward, so every frame
    # would take the fail-mode path. Refuse the combination up front.
    _env(
        monkeypatch, SLASHID_HOOK_ALLOW_UNSIGNED="true", SLASHID_POLICY_URL="https://x/ai-access/a"
    )
    with pytest.raises(ValidationError):
        Config()
