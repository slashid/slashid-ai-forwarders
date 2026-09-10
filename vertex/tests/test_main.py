"""Tests for the Cloud Function entrypoint's source wiring.

``main._sources()`` is nullary and ``@cache``-decorated. That signature
matters — the previous ``_sources(config: Config)`` shape failed
in production the moment ``Config`` gained a ``list[str]`` field
(``audit_observed_models``): pydantic's auto-generated ``__hash__``
hashes the model's raw ``__dict__``, which chokes on the list.

Regression guard: construct a ``Config`` with a populated
``audit_observed_models``, monkeypatch the GCP client factories to
avoid real auth, and call ``_sources()`` twice. Both must return the
same cached list without raising.
"""

from __future__ import annotations

from typing import Any


def test_sources_is_cached_and_config_need_not_be_hashable(
    monkeypatch: Any,
) -> None:
    """Reproduces the ``TypeError: unhashable type: 'list'`` seen in
    Cloud Functions after Phase 3.7 shipped with ``@cache`` on the
    old ``_sources(config)`` signature."""

    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.slashid.com")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t" * 32)
    monkeypatch.setenv("SLASHID_GCP_PROJECT_ID", "smoke-project")
    monkeypatch.setenv("SLASHID_GCP_REGIONS", '["us-central1"]')
    monkeypatch.setenv(
        "SLASHID_AUDIT_OBSERVED_MODELS",
        '["anthropic/claude-sonnet-4-5", "meta/llama-3.3-70b-instruct-maas"]',
    )

    # The @cache on load_config and _sources persists across tests.
    from slashid_vertex_forwarder import config as config_mod
    from slashid_vertex_forwarder import main as main_mod

    config_mod.load_config.cache_clear()
    main_mod._sources.cache_clear()

    # Stub every GCP client factory. Real Firestore/BigQuery/Logging
    # clients need application-default credentials.
    class _Stub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def collection(self, _: str) -> _Stub:
            return self

        def document(self, _: str) -> _Stub:
            return self

    from google.cloud import bigquery, firestore
    from google.cloud import logging as gcp_logging

    monkeypatch.setattr(bigquery, "Client", _Stub)
    monkeypatch.setattr(firestore, "Client", _Stub)
    monkeypatch.setattr(gcp_logging, "Client", _Stub)

    first = main_mod._sources()
    second = main_mod._sources()

    assert first is second, "@cache should return the same list across calls"
    # BQ + audit-only (since audit_observed_models is non-empty above).
    assert len(first) == 2


def test_sources_omits_audit_source_when_observed_models_empty(
    monkeypatch: Any,
) -> None:
    """``audit_observed_models=[]`` disables the audit-only source at
    wiring time — the BQ source is the only entry in the list."""

    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.slashid.com")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t" * 32)
    monkeypatch.setenv("SLASHID_GCP_PROJECT_ID", "smoke-project")
    monkeypatch.setenv("SLASHID_GCP_REGIONS", '["us-central1"]')
    monkeypatch.delenv("SLASHID_AUDIT_OBSERVED_MODELS", raising=False)

    from slashid_vertex_forwarder import config as config_mod
    from slashid_vertex_forwarder import main as main_mod

    config_mod.load_config.cache_clear()
    main_mod._sources.cache_clear()

    class _Stub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def collection(self, _: str) -> _Stub:
            return self

        def document(self, _: str) -> _Stub:
            return self

    from google.cloud import bigquery, firestore
    from google.cloud import logging as gcp_logging

    monkeypatch.setattr(bigquery, "Client", _Stub)
    monkeypatch.setattr(firestore, "Client", _Stub)
    monkeypatch.setattr(gcp_logging, "Client", _Stub)

    sources = main_mod._sources()
    assert len(sources) == 1


def test_sources_builds_one_bq_per_region_plus_one_audit(
    monkeypatch: Any,
) -> None:
    """Multi-region: ``SLASHID_GCP_REGIONS`` (JSON list) expands the
    sources list to one ``BqEventSource`` per region + one global
    ``AuditOnlyEventSource`` covering every region."""

    monkeypatch.setenv("SLASHID_ENDPOINT", "https://api.slashid.com")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t" * 32)
    monkeypatch.setenv("SLASHID_GCP_PROJECT_ID", "smoke-project")
    monkeypatch.setenv(
        "SLASHID_GCP_REGIONS",
        '["us-central1", "europe-west1", "asia-northeast1"]',
    )
    monkeypatch.setenv(
        "SLASHID_AUDIT_OBSERVED_MODELS",
        '["anthropic/claude-sonnet-4-5"]',
    )

    from slashid_vertex_forwarder import config as config_mod
    from slashid_vertex_forwarder import main as main_mod

    config_mod.load_config.cache_clear()
    main_mod._sources.cache_clear()

    class _Stub:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def collection(self, _: str) -> _Stub:
            return self

        def document(self, _: str) -> _Stub:
            return self

    from google.cloud import bigquery, firestore
    from google.cloud import logging as gcp_logging

    monkeypatch.setattr(bigquery, "Client", _Stub)
    monkeypatch.setattr(firestore, "Client", _Stub)
    monkeypatch.setattr(gcp_logging, "Client", _Stub)

    sources = main_mod._sources()
    # 3 BQ (one per region) + 1 audit = 4.
    assert len(sources) == 4

    from slashid_vertex_forwarder.audit_only_source import AuditOnlyEventSource
    from slashid_vertex_forwarder.event_source import BqEventSource

    bq_sources = [s for s in sources if isinstance(s, BqEventSource)]
    audit_sources = [s for s in sources if isinstance(s, AuditOnlyEventSource)]
    assert len(bq_sources) == 3
    assert len(audit_sources) == 1
    # BQ sources carry per-region datasets with the naming convention.
    assert {s._region for s in bq_sources} == {
        "us-central1",
        "europe-west1",
        "asia-northeast1",
    }
    assert {s._dataset_id for s in bq_sources} == {
        "slashid_vertex_reqresp_logs_us_central1",
        "slashid_vertex_reqresp_logs_europe_west1",
        "slashid_vertex_reqresp_logs_asia_northeast1",
    }
    # The audit source's regions list covers every observed region.
    assert set(audit_sources[0]._regions) == {
        "us-central1",
        "europe-west1",
        "asia-northeast1",
    }
