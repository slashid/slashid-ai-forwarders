from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from slashid_ai_forwarder_core.events import AIInvocationObservedV1
from slashid_ai_forwarder_core.platform import Checkpoint

from slashid_openai_forwarder import uploads
from slashid_openai_forwarder.compliance import ComplianceError, LogFile, LogPage
from slashid_openai_forwarder.config import Config

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _line(event_id: str, *, author: str = "user", files: list | None = None, **kw: object) -> bytes:
    message: dict = {
        "id": f"m-{event_id}",
        "created_at": "2026-11-01T12:00:01Z",
        "author": {"type": author},
        "content": {"type": "text", "value": "see attached"},
    }
    if files is not None:
        message["files"] = files
    return json.dumps(
        {
            "event_id": event_id,
            "type": "CONVERSATION_MESSAGE",
            "timestamp": "2026-11-01T12:00:01Z",
            "actor": {"type": "ACCOUNT_USER", "user_id": "user-a", "user_email": "a@x.com"},
            "message": message,
            "conversation": {"id": "c1"},
            **kw,
        }
    ).encode()


FILE = [{"id": "file-1", "name": "Q3 board deck.pdf"}]


def test_only_a_users_message_with_files_is_an_upload() -> None:
    events = uploads.events_in(
        [
            _line("e1", files=FILE),
            _line("e2"),
            _line("e3", author="assistant", files=FILE),
            b"not json",
        ],
        set(),
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.request_id == "chatgpt:c1:m-e1"
    assert ev.identity_details.model_dump(exclude_none=True) == {
        "kind": "openai",
        "user_id": "user-a",
    }
    assert ev.accessed_files is not None
    assert [(f.name, f.provenance) for f in ev.accessed_files] == [
        ("Q3 board deck.pdf", "attachment")
    ]
    assert ev.timestamp == "2026-11-01T12:00:01.000Z"


def test_a_line_delivered_twice_is_reported_once() -> None:
    seen: set[str] = set()
    assert len(uploads.events_in([_line("e1", files=FILE)], seen)) == 1
    assert uploads.events_in([_line("e1", files=FILE)], seen) == []


class _Store:
    def __init__(self, saved: Checkpoint | None = None) -> None:
        self.saved = saved or Checkpoint(timestamp=None, id=None)
        self.history: list[Checkpoint] = []

    async def load(self) -> Checkpoint:
        return self.saved

    async def save(self, checkpoint: Checkpoint) -> None:
        self.saved = checkpoint
        self.history.append(checkpoint)


class _Compliance:
    def __init__(self, pages: list[LogPage], files: dict[str, list[bytes] | Exception]) -> None:
        self.pages = pages
        self.files = files
        self.afters: list[datetime] = []

    async def log_files(self, *, after: datetime) -> LogPage:
        self.afters.append(after)
        return self.pages.pop(0) if self.pages else LogPage(data=[])

    async def log_lines(self, file_id: str) -> list[bytes]:
        out = self.files[file_id]
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> list[AIInvocationObservedV1]:
    out: list[AIInvocationObservedV1] = []

    async def push(_http: object, events: list, **_kw: object) -> int:
        out.extend(events)
        return len(events)

    monkeypatch.setattr(uploads, "push_invocations", push)
    return out


def _config() -> Config:
    return Config(push_token="t" * 40, compliance_key="k", workspace_id="w")


async def test_a_cold_start_begins_now_and_saves_after_each_file(pushed: list) -> None:
    store = _Store()
    t1, t2 = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    api = _Compliance(
        [
            LogPage(
                data=[LogFile(id="f1", end_time=t1), LogFile(id="f2", end_time=t2)],
                has_more=False,
                last_end_time=t2,
            )
        ],
        {"f1": [_line("e1", files=FILE)], "f2": [_line("e2")]},
    )
    async with httpx.AsyncClient() as http:
        out = await uploads.read_uploads(
            config=_config(), compliance=api, checkpoint=store, http=http, now=T0
        )
    assert out == {"log_files": 2, "uploads": 1}
    assert api.afters == [T0]
    assert [c.id for c in store.history] == [None, "f1", "f2"]
    assert store.saved == Checkpoint(timestamp=t2, id="f2")
    assert len(pushed) == 1


async def test_the_file_the_watermark_names_is_not_read_again(pushed: list) -> None:
    store = _Store(Checkpoint(timestamp=T0, id="f1"))
    api = _Compliance(
        [LogPage(data=[LogFile(id="f1", end_time=T0)], has_more=True, last_end_time=T0)],
        {"f1": [_line("e1", files=FILE)]},
    )
    async with httpx.AsyncClient() as http:
        out = await uploads.read_uploads(
            config=_config(), compliance=api, checkpoint=store, http=http, now=T0
        )
    assert out == {"log_files": 0, "uploads": 0}
    assert pushed == []


async def test_a_download_failure_holds_the_watermark_before_that_file(pushed: list) -> None:
    store = _Store(Checkpoint(timestamp=T0, id=None))
    t1 = T0 + timedelta(minutes=1)
    api = _Compliance(
        [
            LogPage(
                data=[LogFile(id="f1", end_time=t1), LogFile(id="f2", end_time=t1)],
                last_end_time=t1,
            )
        ],
        {"f1": [_line("e1", files=FILE)], "f2": ComplianceError("GET /logs/f2: HTTP 500")},
    )
    async with httpx.AsyncClient() as http:
        out = await uploads.read_uploads(
            config=_config(), compliance=api, checkpoint=store, http=http, now=T0
        )
    assert out == {"log_files": 1, "uploads": 1}
    assert store.saved == Checkpoint(timestamp=t1, id="f1")
