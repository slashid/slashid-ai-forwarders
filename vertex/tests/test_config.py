"""Config env-var parsing."""

from __future__ import annotations

import pytest

from slashid_vertex_forwarder.config import Config


def _env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    base = {
        "SLASHID_ENDPOINT": "http://test",
        "SLASHID_PUSH_TOKEN": "token",
        "SLASHID_GCP_PROJECT_ID": "vertex-test-507702",
        "SLASHID_GCP_REGIONS": '["us-central1"]',
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)


def test_required_fields_populate_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.gcp_project_id == "vertex-test-507702"
    assert cfg.gcp_regions == ["us-central1"]
    assert cfg.endpoint == "http://test"
    assert cfg.push_token == "token"


def test_dataset_and_checkpoint_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.bq_dataset_prefix == "slashid_vertex_reqresp_logs"
    assert cfg.firestore_database == "slashid-vertex"
    assert cfg.firestore_checkpoint_collection == "slashid_vertex"
    assert cfg.max_rows_per_tick == 1000


def test_dataset_and_checkpoint_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(
        monkeypatch,
        SLASHID_BQ_DATASET_PREFIX="custom_dataset",
        SLASHID_FIRESTORE_DATABASE="custom-db",
        SLASHID_FIRESTORE_CHECKPOINT_COLLECTION="custom_col",
        SLASHID_MAX_ROWS_PER_TICK="500",
    )
    cfg = Config()
    assert cfg.bq_dataset_prefix == "custom_dataset"
    assert cfg.firestore_database == "custom-db"
    assert cfg.firestore_checkpoint_collection == "custom_col"
    assert cfg.max_rows_per_tick == 500


def test_gcp_regions_multi_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SLASHID_GCP_REGIONS`` accepts a JSON list with multiple entries."""
    _env(
        monkeypatch,
        SLASHID_GCP_REGIONS='["us-central1", "europe-west1"]',
    )
    cfg = Config()
    assert cfg.gcp_regions == ["us-central1", "europe-west1"]


def test_gcp_regions_missing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SLASHID_GCP_REGIONS`` unset → validation fails at load."""
    monkeypatch.setenv("SLASHID_ENDPOINT", "http://test")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "token")
    monkeypatch.setenv("SLASHID_GCP_PROJECT_ID", "vertex-test-507702")
    monkeypatch.delenv("SLASHID_GCP_REGIONS", raising=False)
    with pytest.raises(ValueError, match="at least one region"):
        Config()


def test_endpoint_trailing_slash_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SLASHID_ENDPOINT="http://test/")
    cfg = Config()
    assert cfg.endpoint == "http://test"


def test_config_audit_observed_models_default_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    cfg = Config()
    assert cfg.audit_observed_models == []


def test_config_audit_observed_models_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(
        monkeypatch,
        SLASHID_AUDIT_OBSERVED_MODELS='["anthropic/claude-sonnet-4-5","meta/llama-3.3-70b-instruct-maas"]',
    )
    cfg = Config()
    assert cfg.audit_observed_models == [
        "anthropic/claude-sonnet-4-5",
        "meta/llama-3.3-70b-instruct-maas",
    ]
