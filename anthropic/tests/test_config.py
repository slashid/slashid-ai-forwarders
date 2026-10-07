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
        "SLASHID_PROJECT_ID": "proj",
        "SLASHID_PLATFORM": "gcp",
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
    assert cfg.preflight_enabled is False
    assert cfg.capture_bucket is None


def test_fail_mode_must_be_allow_or_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_VERDICT_FAIL_MODE="maybe")
    with pytest.raises(ValidationError):
        Config()


def test_a_capability_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_HOOK_SIGNING_SECRET")
    with pytest.raises(ValidationError, match="no capability configured"):
        Config()


def test_compliance_is_off_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    assert Config().compliance_enabled is False


def test_a_compliance_key_turns_the_readers_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # The organization uuid rides along because the key alone is refused:
    # it reads every linked organization and the readers filter to one.
    _env(monkeypatch, SLASHID_COMPLIANCE_KEY="sk-ant-api01-x", SLASHID_ORGANIZATION_UUID="org-1")
    assert Config().compliance_enabled is True


def test_the_store_knobs_have_the_designed_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    config = Config()
    assert config.join_wait_seconds == 3600
    assert config.tombstone_ttl_seconds == 7200
    assert config.pending_collection == "anthropic_pending"
    assert config.database == "slashid-anthropic"
    assert config.max_flushes_per_tick == 500
    assert config.project_id == "proj"
    # Fail closed: with no scheduler identity named, every tick is refused.
    assert config.tick_principal is None
    assert config.tick_audience is None


def test_the_project_id_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Firestore client built with ``project=None`` fails on the first
    write, in a background task whose exception reaches no response."""
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_PROJECT_ID")
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
        SLASHID_COMPLIANCE_KEY="",
        SLASHID_ORGANIZATION_UUID="",
        SLASHID_CAPTURE_BUCKET="",
        SLASHID_CAPTURE_DENY_MARKER="",
    )
    cfg = Config()
    assert cfg.compliance_key is None
    assert cfg.organization_uuid is None
    assert cfg.capture_bucket is None
    assert cfg.capture_deny_marker is None


def test_compliance_only_starts_without_a_signing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The conflict the design names: the old validator made the
    compliance-only deployment impossible to start."""
    _env(monkeypatch, SLASHID_COMPLIANCE_KEY="sk-ant-api01-x", SLASHID_ORGANIZATION_UUID="org-1")
    monkeypatch.delenv("SLASHID_HOOK_SIGNING_SECRET")
    cfg = Config()
    assert cfg.hook_enabled is False
    assert cfg.compliance_enabled is True


def test_hook_only_needs_no_compliance_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.hook_enabled is True
    assert cfg.compliance_enabled is False


def test_no_credential_at_all_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_HOOK_SIGNING_SECRET")
    with pytest.raises(ValidationError, match="no capability configured"):
        Config()


def test_compliance_key_requires_an_organization_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key reads every linked organization; the readers filter to one."""
    _env(monkeypatch, SLASHID_COMPLIANCE_KEY="sk-ant-api01-x")
    with pytest.raises(ValidationError, match="SLASHID_ORGANIZATION_UUID"):
        Config()


def test_tombstone_ttl_must_outlive_join_wait_poll_lag_and_one_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3600 + 120 + 3600 > 7200: an hourly tick at the default join wait is
    exactly the configuration the assertion exists to refuse."""
    _env(monkeypatch, SLASHID_TICK_INTERVAL_SECONDS="3600")
    with pytest.raises(ValidationError, match="SLASHID_TOMBSTONE_TTL_SECONDS"):
        Config()
    _env(monkeypatch, SLASHID_TICK_INTERVAL_SECONDS="3600", SLASHID_TOMBSTONE_TTL_SECONDS="10800")
    assert Config().tombstone_ttl_seconds == 10800


def test_a_local_platform_needs_no_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_PLATFORM="local")
    monkeypatch.delenv("SLASHID_PROJECT_ID")
    config = Config()
    assert config.platform == "local"
    assert config.project_id is None
    assert config.data_dir is None
    monkeypatch.setenv("SLASHID_DATA_DIR", "/x")
    assert Config().data_dir == "/x"


def test_gcp_refuses_an_empty_project_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_PROJECT_ID="")
    with pytest.raises(ValidationError, match="SLASHID_PROJECT_ID"):
        Config()


def test_an_empty_data_dir_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_PLATFORM="local", SLASHID_DATA_DIR="/x")
    assert Config().data_dir == "/x"
    monkeypatch.setenv("SLASHID_DATA_DIR", "")
    assert Config().data_dir is None


def test_an_unknown_platform_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_PLATFORM="azure")
    with pytest.raises(ValidationError):
        Config()


def test_local_is_the_default_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    monkeypatch.delenv("SLASHID_PLATFORM")
    monkeypatch.delenv("SLASHID_PROJECT_ID")
    assert Config().platform == "local"
