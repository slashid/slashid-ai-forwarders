"""The tick route and the wiring of sources.

``create_app`` takes every stateful piece as an argument, so the route is
tested with fakes over ASGI; ``_sources`` is tested with stubbed Google
clients, since real ones need application-default credentials.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.platform import Checkpoint

from slashid_vertex_forwarder import handler
from slashid_vertex_forwarder import main as vertex_main
from slashid_vertex_forwarder.audit_only_source import AuditOnlyEventSource
from slashid_vertex_forwarder.config import Config
from slashid_vertex_forwarder.event_source import BqEventSource
from slashid_vertex_forwarder.main import Backends, _sources, create_app, open_backends
from tests.test_bq_event_source import _row, _source
from tests.test_handler import _config, _event


class _FakeSource:
    def __init__(
        self,
        events: list[AIInvocationObservedV1] | None = None,
        *,
        raise_on_fetch: Exception | None = None,
    ) -> None:
        self._events = events or []
        self._raise = raise_on_fetch
        self.fetch_count = 0
        self.commits: list[Checkpoint] = []

    async def fetch(self) -> tuple[list[AIInvocationObservedV1], Checkpoint | None]:
        self.fetch_count += 1
        if self._raise is not None:
            raise self._raise
        return list(self._events), Checkpoint(None, "7") if self._events else None

    async def commit(self, checkpoint: Checkpoint) -> None:
        self.commits.append(checkpoint)


class _FakeLease:
    def __init__(self, *, held: bool = True) -> None:
        self._held = held
        self.durations: list[timedelta] = []

    @contextlib.asynccontextmanager
    async def hold(self, lease: timedelta) -> AsyncIterator[bool]:
        self.durations.append(lease)
        yield self._held


async def _accept(token: str) -> bool:
    return token == "good"


async def _post(app: Any, *, token: str | None = "good") -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/tick", headers=headers)


@pytest.fixture(autouse=True)
def _push(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    pushed: list[Any] = []

    async def _fake_push(_client: Any, events: list[Any], **_kw: Any) -> int:
        pushed.extend(events)
        return len(events)

    monkeypatch.setattr(handler, "push_invocations", _fake_push)
    return pushed


async def test_a_tick_without_a_token_is_refused() -> None:
    source = _FakeSource()
    app = create_app(_config(), sources=[source], tick_auth=_accept)
    assert (await _post(app, token=None)).status_code == 401
    assert source.fetch_count == 0


async def test_a_refused_token_is_401() -> None:
    source = _FakeSource()
    app = create_app(_config(), sources=[source], tick_auth=_accept)
    assert (await _post(app, token="bad")).status_code == 401
    assert source.fetch_count == 0


async def test_an_unset_scheduler_principal_refuses_every_token() -> None:
    source = _FakeSource()
    app = create_app(_config(), sources=[source])  # no tick_auth: fail closed
    assert (await _post(app)).status_code == 401
    assert source.fetch_count == 0


async def test_a_held_lease_skips_the_tick() -> None:
    source = _FakeSource([_event()])
    lease = _FakeLease(held=False)
    app = create_app(_config(), sources=[source], lease=lease, tick_auth=_accept)
    response = await _post(app)
    assert (response.status_code, response.json()) == (200, {"skipped": True})
    assert source.fetch_count == 0


async def test_a_tick_returns_the_counters_and_commits(_push: list[Any]) -> None:
    source = _FakeSource([_event()])
    lease = _FakeLease()
    app = create_app(_config(), sources=[source], lease=lease, tick_auth=_accept)
    response = await _post(app)
    assert (response.status_code, response.json()) == (
        200,
        {"events_pushed": 1, "envelopes_seen": 1},
    )
    assert source.commits == [Checkpoint(None, "7")]
    assert len(_push) == 1
    assert lease.durations and lease.durations[0] >= timedelta(minutes=9)


async def test_one_source_failing_does_not_stop_the_next() -> None:
    broken = _FakeSource(raise_on_fetch=RuntimeError("bigquery is down"))
    healthy = _FakeSource([_event()])
    app = create_app(_config(), sources=[broken, healthy], tick_auth=_accept)
    response = await _post(app)
    assert response.status_code == 200
    assert healthy.fetch_count == 1 and healthy.commits == [Checkpoint(None, "7")]
    assert broken.commits == []


async def test_a_bigquery_failure_through_tick_commits_nothing_and_the_next_source_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bq, client, store = _source(rows=[_row()])

    def _explode(query: str, job_config: Any) -> Any:
        raise RuntimeError("bigquery is down")

    monkeypatch.setattr(client, "query", _explode)
    healthy = _FakeSource([_event()])
    app = create_app(_config(), sources=[bq, healthy], tick_auth=_accept)
    response = await _post(app)
    assert response.status_code == 200
    assert store.saves == []
    assert healthy.fetch_count == 1 and healthy.commits == [Checkpoint(None, "7")]


async def test_the_platform_is_opened_at_startup_used_by_the_tick_and_closed_at_shutdown() -> None:
    source = _FakeSource([_event()])
    lease = _FakeLease()
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[Backends]:
        events.append("open")
        yield Backends(sources=[source], lease=lease, tick_auth=_accept)
        events.append("close")

    app = create_app(_config(), backends=opened)
    async with app.router.lifespan_context(app):
        assert events == ["open"]
        response = await _post(app)
        # the opened source, lease and auth did the work, not nothing given up front
        assert (response.status_code, response.json()) == (
            200,
            {"events_pushed": 1, "envelopes_seen": 1},
        )
        assert source.commits == [Checkpoint(None, "7")] and lease.durations
    assert events == ["open", "close"]


def test_create_app_needs_sources_or_backends() -> None:
    with pytest.raises(TypeError, match="sources or backends"):
        create_app(_config())


async def test_a_tick_before_the_backends_are_opened_is_unavailable() -> None:
    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[Backends]:
        yield Backends(sources=[], lease=None, tick_auth=_accept)

    app = create_app(_config(), backends=opened)  # lifespan not run
    # the default auth refuses first, which is the safe answer too
    assert (await _post(app)).status_code in (401, 503)


async def test_open_backends_opens_the_configured_platform_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_google(monkeypatch)
    events: list[Any] = []

    @contextlib.asynccontextmanager
    async def get(name: str, **options: Any) -> AsyncIterator[Any]:
        events.append(("open", name, options))
        yield _StubPlatform()
        events.append("close")

    monkeypatch.setattr(vertex_main.platforms, "get", get)
    config = _config_for(["us-central1"], [])
    async with open_backends(config) as backends:
        assert events == [
            ("open", "gcp", {"project": "smoke-project", "firestore_database": "slashid-vertex"})
        ]
        assert len(backends.sources) == 1
        assert backends.tick_auth is _accept
    assert events[-1] == "close"


# --- source wiring -----------------------------------------------------------


class _StubClient:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass


class _StubPlatform:
    def __init__(self) -> None:
        self.documents: list[tuple[str, str]] = []

    def checkpoint_store(self, *, collection: str, document: str) -> Any:
        self.documents.append((collection, document))
        return object()

    def tick_lease(self, *, collection: str, document: str) -> Any:
        return _FakeLease()

    def scheduler_auth(self, *, principal: str | None, audience: str | None) -> Any:
        return _accept


def _stub_google(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.cloud import bigquery
    from google.cloud import logging as gcp_logging

    monkeypatch.setattr(bigquery, "Client", _StubClient)
    monkeypatch.setattr(gcp_logging, "Client", _StubClient)


def _config_for(regions: list[str], observed: list[str]) -> Config:
    return Config(
        endpoint="https://api.slashid.com",
        push_token="t" * 32,
        project_id="smoke-project",
        gcp_regions=regions,
        audit_observed_models=observed,
    )


def test_sources_omits_the_audit_source_when_no_models_are_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_google(monkeypatch)
    sources = _sources(_config_for(["us-central1"], []), _StubPlatform())  # ty: ignore[invalid-argument-type]
    assert len(sources) == 1 and isinstance(sources[0], BqEventSource)


def test_sources_builds_one_bq_per_region_plus_one_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_google(monkeypatch)
    platform = _StubPlatform()
    regions = ["us-central1", "europe-west1", "asia-northeast1"]
    sources = _sources(
        _config_for(regions, ["anthropic/claude-sonnet-4-5"]),
        platform,  # ty: ignore[invalid-argument-type]
    )
    bq = [s for s in sources if isinstance(s, BqEventSource)]
    audit = [s for s in sources if isinstance(s, AuditOnlyEventSource)]
    assert (len(bq), len(audit)) == (3, 1)
    assert {s._dataset_id for s in bq} == {
        "slashid_vertex_reqresp_logs_us_central1",
        "slashid_vertex_reqresp_logs_europe_west1",
        "slashid_vertex_reqresp_logs_asia_northeast1",
    }
    assert set(audit[0]._regions) == set(regions)
    # One checkpoint document per source, so watermarks cannot collide.
    assert len({document for _, document in platform.documents}) == 4
