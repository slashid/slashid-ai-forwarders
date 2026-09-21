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
        "SLASHID_GCP_PROJECT_ID": "proj",
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
    assert cfg.preflight_enabled is False
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


def test_compliance_is_off_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert Config().compliance_enabled is False


def test_a_compliance_key_turns_the_readers_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_COMPLIANCE_KEY="sk-ant-api01-x")
    assert Config().compliance_enabled is True


def test_the_store_knobs_have_the_designed_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    config = Config()
    assert config.join_wait_seconds == 3600
    assert config.tombstone_ttl_seconds == 7200
    assert config.pending_collection == "anthropic_pending"
    assert config.firestore_database == "slashid-anthropic"
    assert config.max_flushes_per_tick == 500
    assert config.gcp_project_id == "proj"
    # Fail closed: with no scheduler identity named, every tick is refused.
    assert config.tick_service_account is None
    assert config.tick_audience is None


def test_the_project_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Firestore client built with ``project=None`` fails on the first
    write, in a background task whose exception reaches no response."""
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_GCP_PROJECT_ID")
    with pytest.raises(ValidationError):
        Config()


def test_the_reader_knobs_have_the_designed_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    config = Config()
    assert config.organization_uuid is None
    assert config.poll_lag_seconds == 120
    assert config.max_sessions_per_tick == 200
    assert config.attachment_hashing == "md5"
    assert config.max_attachment_fetch_bytes == 10 * 1024 * 1024
    assert config.checkpoint_collection == "anthropic_checkpoints"
    assert config.soft_join_window_seconds == 15


def test_attachment_hashing_must_be_md5_or_full(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_ATTACHMENT_HASHING="sha256")
    with pytest.raises(ValidationError):
        Config()


def test_mock_denied_hashes_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert Config().denied_hashes == ()


def test_mock_denied_hashes_parse_like_the_signing_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One splitter, not two: same trimming and same empty-skipping as
    SLASHID_HOOK_SIGNING_SECRET, and lowercased so a digest pasted from a
    tool that prints uppercase still matches ours."""
    _env(monkeypatch, SLASHID_MOCK_DENIED_HASHES=" ABC123 , ,def456 ")
    assert Config().denied_hashes == ("abc123", "def456")


def test_the_tick_interval_has_the_designed_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declared as a number because the startup assertion has to compare it
    against the tombstone TTL, and a cron string will not do."""
    _env(monkeypatch)
    assert Config().tick_interval_seconds == 300


def test_empty_strings_from_terraform_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module always sets every env var it manages, ``""`` for the ones a
    deployment leaves out. An empty compliance key must not switch the
    readers on."""
    _env(
        monkeypatch,
        SLASHID_POLICY_URL="",
        SLASHID_COMPLIANCE_KEY="",
        SLASHID_ORGANIZATION_UUID="",
        SLASHID_CAPTURE_BUCKET="",
        SLASHID_CAPTURE_DENY_MARKER="",
    )
    cfg = Config()
    assert cfg.policy_url is None
    assert cfg.compliance_key is None
    assert cfg.organization_uuid is None
    assert cfg.capture_bucket is None
    assert cfg.capture_deny_marker is None
