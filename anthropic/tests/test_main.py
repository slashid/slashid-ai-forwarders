"""The HTTP surface: signature gate, verdict body, capture, failure isolation."""

from __future__ import annotations

import json
import pathlib
from datetime import timedelta
from typing import Any

import httpx
import pytest

from slashid_anthropic_forwarder import main
from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.main import create_app
from slashid_anthropic_forwarder.pending import TICK_LEASE
from slashid_anthropic_forwarder.store import FirestorePendingStore, FirestoreTickLease, TickLease
from tests.conftest import SECRET, Signer
from tests.fake_firestore import FakeFirestore
from tests.test_pending import ADDRESS, Sink, a_store, addresses, fake, seed

FRAME: dict[str, Any] = {
    "type": "prompt",
    "request_id": "req_test",
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "actor": {"type": "user", "id": "user_01A", "email_address": "a@example.com"},
    "source": {"application": "claude-code"},
    "session_id": None,
    "model": "claude-sonnet-4-5",
    "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
    "metadata": {},
}


class MemorySink:
    def __init__(self, *, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = fail

    async def put(self, name: str, data: bytes, *, content_type: str) -> None:
        if self.fail:
            raise RuntimeError("bucket unreachable")
        self.objects[name] = data


def _config(**overrides: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.com",
        "push_token": "tok",
        "hook_signing_secret": SECRET,
        "gcp_project_id": "proj",
    }
    base.update(overrides)
    return Config(**base)


SCHEDULER = {"authorization": "Bearer scheduler-token"}


async def _accepts_the_scheduler(token: str) -> bool:
    """Stands in for `GcpPlatform.scheduler_auth`, which verifies a Google
    signature against Google's certificates and cannot run offline."""
    return token == "scheduler-token"


def _client(
    config: Config,
    capture: MemorySink | None = None,
    store: FirestorePendingStore | None = None,
    sink: Sink | None = None,
    lease: TickLease | None = None,
) -> httpx.AsyncClient:
    app = create_app(
        config,
        capture=capture,
        store=store,
        lease=lease,
        tick_auth=_accepts_the_scheduler,
        client=(sink or Sink()).client(),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


TOOL_FRAME = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "frame_tool_result.json").read_text()
)


async def test_unsigned_request_is_rejected() -> None:
    async with _client(_config()) as c:
        r = await c.post("/", content=json.dumps(FRAME).encode())
    assert r.status_code == 401


async def test_signed_prompt_frame_is_allowed(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    async with _client(_config()) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_frame_is_captured_raw_with_its_headers(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    capture = MemorySink()
    async with _client(_config(capture_bucket="b"), capture) as c:
        await c.post("/", content=body, headers=sign(body, "req_test"))
    [(name, data)] = capture.objects.items()
    stored = json.loads(data)
    assert name.endswith("_req_test.json")
    assert stored["body"].encode() == body  # the raw frame, not a re-encoding
    assert stored["headers"]["webhook-id"] == "req_test"


async def test_capture_failure_never_reaches_the_verdict(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    async with _client(_config(capture_bucket="b"), MemorySink(fail=True)) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_unknown_top_level_type_is_allowed(sign: Signer) -> None:
    body = json.dumps({**FRAME, "type": "response"}).encode()
    async with _client(_config()) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_unparseable_body_is_still_allowed(sign: Signer) -> None:
    # A rejected body is a webhook failure; the frame is inspected, not parsed
    # for the verdict, so answer allow rather than 400.
    body = b"not json"
    async with _client(_config()) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_deny_marker_denies_only_outside_shadow_mode(sign: Signer) -> None:
    text = {"type": "text", "text": "SLASHID_DENY_ME"}
    frame = {**FRAME, "messages": [{"role": "user", "content": [text]}]}
    body = json.dumps(frame).encode()
    async with _client(_config(capture_deny_marker="SLASHID_DENY_ME")) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.json() == {"action": "allow"}
    async with _client(_config(capture_deny_marker="SLASHID_DENY_ME", shadow_mode=False)) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200
    verdict = r.json()
    assert verdict["action"] == "deny"
    assert verdict["deny_reason"]
    assert len(verdict["reference_id"]) == 32


async def test_oversized_body_is_rejected(sign: Signer) -> None:
    body = json.dumps(FRAME).encode()
    async with _client(_config(max_body_bytes=10)) as c:
        r = await c.post("/", content=body, headers=sign(body, "req_test"))
    assert r.status_code == 413


@pytest.mark.parametrize("path", ["/", "/hooks/anthropic"])
async def test_any_path_is_the_endpoint(sign: Signer, path: str) -> None:
    # Anthropic posts to whatever URL the admin configured; no fixed suffix.
    body = json.dumps(FRAME).encode()
    async with _client(_config()) as c:
        r = await c.post(path, content=body, headers=sign(body, "req_test"))
    assert r.status_code == 200


async def test_a_delivery_writes_its_records(sign: Signer) -> None:
    body = json.dumps(TOOL_FRAME).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert any(a.startswith("tail:") for a in addresses(store))


async def test_a_store_failure_never_reaches_the_verdict(sign: Signer) -> None:
    """Rule 1: a non-200 is a webhook failure, and enough of them disable
    enforcement for the whole organization."""

    class Broken(FirestorePendingStore):
        async def upsert(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("firestore unreachable")

    store = Broken(client=FakeFirestore(), collection="c", join_wait=timedelta(hours=1))
    body = json.dumps(TOOL_FRAME).encode()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.status_code == 200
    assert r.json() == {"action": "allow"}


async def test_the_record_is_written_after_the_response_is_sent(sign: Signer) -> None:
    """Not merely isolated from the verdict — it does not delay it."""
    order: list[str] = []

    class Noted(FirestorePendingStore):
        async def upsert(self, *args: Any, **kwargs: Any) -> Any:
            order.append("store")
            return await super().upsert(*args, **kwargs)

    store = Noted(client=FakeFirestore(), collection="c", join_wait=timedelta(hours=1))
    body = json.dumps(TOOL_FRAME).encode()
    headers = sign(body, "msg_1")
    app = create_app(_config(), store=store, client=Sink().client())

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Any) -> None:  # ASGI's Send takes a MutableMapping
        if message["type"] == "http.response.body":
            order.append("response")

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/",
            "raw_path": b"/",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "client": ("127.0.0.1", 1),
            "server": ("t", 80),
        },
        receive,
        send,
    )
    assert order[0] == "response" and "store" in order


async def test_a_config_test_frame_writes_nothing(sign: Signer) -> None:
    body = json.dumps({**TOOL_FRAME, "source": {"application": "config-test"}}).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert addresses(store) == set()


async def test_an_unknown_type_writes_nothing(sign: Signer) -> None:
    body = json.dumps({**TOOL_FRAME, "type": "response"}).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert addresses(store) == set()


async def test_the_tick_needs_no_signature() -> None:
    """Cloud Scheduler carries an OIDC token and none of the webhook
    headers, so the signature gate must not run here."""
    async with _client(_config(), store=a_store()) as c:
        r = await c.post("/tick", headers=SCHEDULER)
    assert r.status_code == 200
    assert r.json() == {"flushed": 0}


async def test_an_unauthenticated_tick_is_refused() -> None:
    """`allUsers` holds roles/run.invoker on a service the hook can reach,
    so this token is the only thing between an anonymous caller and the
    reader pass."""
    store = a_store(join_wait=timedelta(seconds=-1))
    await seed(store)
    sink = Sink()
    async with _client(_config(), store=store, sink=sink) as c:
        assert (await c.post("/tick")).status_code == 401
        assert (await c.post("/tick", headers={"authorization": "Bearer nope"})).status_code == 401
        assert (
            await c.post("/tick", headers={"authorization": "scheduler-token"})
        ).status_code == 401
    assert sink.bodies == []


async def test_the_tick_flushes_records_past_their_deadline() -> None:
    store = a_store(join_wait=timedelta(seconds=-1))  # born already due
    await seed(store)
    sink = Sink()
    async with _client(_config(), store=store, sink=sink) as c:
        r = await c.post("/tick", headers=SCHEDULER)
    assert r.json() == {"flushed": 1}
    assert sink.request_ids == [ADDRESS]


async def test_a_tick_that_finds_the_lease_held_does_no_work() -> None:
    """The flush would survive the overlap; the readers this route grows in
    Chunk 7 would not, and their checkpoint has no precondition."""
    store = a_store(join_wait=timedelta(seconds=-1))
    await seed(store)
    lease = FirestoreTickLease(client=fake(store), collection="anthropic_pending")
    assert await lease.take(TICK_LEASE, owner="the-tick-already-running") is True
    sink = Sink()
    async with _client(_config(), store=store, sink=sink, lease=lease) as c:
        r = await c.post("/tick", headers=SCHEDULER)
    assert r.json() == {"flushed": 0, "skipped": True}
    assert sink.bodies == []


async def test_a_tick_releases_the_lease_so_the_next_one_runs() -> None:
    store = a_store(join_wait=timedelta(seconds=-1))
    await seed(store)
    lease = FirestoreTickLease(client=fake(store), collection="anthropic_pending")
    sink = Sink()
    async with _client(_config(), store=store, sink=sink, lease=lease) as c:
        assert (await c.post("/tick", headers=SCHEDULER)).json() == {"flushed": 1}
        assert (await c.post("/tick", headers=SCHEDULER)).json() == {"flushed": 0}
    assert sink.request_ids == [ADDRESS]


async def test_a_delivery_posted_to_slash_tick_is_still_a_delivery(sign: Signer) -> None:
    """A customer whose webhook URL ends in /tick would otherwise have every
    frame swallowed by the scheduler route."""
    body = json.dumps(TOOL_FRAME).encode()
    store = a_store()
    async with _client(_config(), store=store) as c:
        r = await c.post("/tick", content=body, headers=sign(body, "msg_1"))
    assert r.json() == {"action": "allow"}
    assert addresses(store) != set()


async def test_tick_runs_the_readers_before_the_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []

    async def fake_readers(**kwargs: Any) -> dict[str, int]:
        order.append("readers")
        return {"responses_emitted": 2}

    async def fake_flush(*args: Any, **kwargs: Any) -> int:
        order.append("flush")
        return 1

    monkeypatch.setattr(main, "run_readers", fake_readers)
    monkeypatch.setattr(main, "flush_due", fake_flush)
    async with _client(_config(), store=a_store()) as c:
        response = await c.post("/tick", headers=SCHEDULER)
    assert order == ["readers", "flush"]
    assert response.json() == {"flushed": 1, "responses_emitted": 2}


async def test_a_reader_failure_does_not_fail_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**kwargs: Any) -> dict[str, int]:
        raise RuntimeError("firestore down")

    monkeypatch.setattr(main, "run_readers", boom)
    async with _client(_config(), store=a_store()) as c:
        response = await c.post("/tick", headers=SCHEDULER)
    # A reader failure is noise on one tick: the next cron fire picks up
    # from the same watermark. (Cloud Scheduler is configured with no
    # retries, so nothing re-runs this tick — the flush behind it still
    # ran, which is the part that matters.)
    assert response.status_code == 200
    assert response.json()["flushed"] == 0
