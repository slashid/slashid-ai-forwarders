"""``GcpPlatform``: what it hands out, without touching Google."""

from __future__ import annotations

from typing import Any

from slashid_ai_forwarder_core.checkpoint import Checkpoint
from slashid_ai_forwarder_core.platform.gcp import GcpPlatform


class _Doc:
    def __init__(self, backing: dict[str, Any], key: str) -> None:
        self._backing, self._key = backing, key

    def get(self) -> Any:
        data = self._backing.get(self._key)
        return type("Snap", (), {"exists": data is not None, "to_dict": lambda _: data})()

    def set(self, data: dict[str, Any]) -> None:
        self._backing[self._key] = data


class _FakeFirestoreClient:
    def __init__(self) -> None:
        self.docs: dict[str, Any] = {}

    def collection(self, name: str) -> Any:
        docs = self.docs
        return type("Col", (), {"document": lambda _, d: _Doc(docs, f"{name}/{d}")})()


def _platform(client: Any) -> GcpPlatform:
    platform = GcpPlatform(project="p", database="d")
    platform.__dict__["firestore"] = client  # what the cached property would build
    return platform


def test_checkpoint_stores_share_the_client_and_keep_their_own_documents() -> None:
    client = _FakeFirestoreClient()
    platform = _platform(client)
    a = platform.checkpoint_store(collection="c", document="a")
    b = platform.checkpoint_store(collection="c", document="b")
    a.save(Checkpoint(timestamp=None, id="x"))
    assert b.load() == Checkpoint(timestamp=None, id=None)
    assert a.load().id == "x"


async def test_scheduler_auth_without_a_principal_refuses_every_token() -> None:
    check = _platform(_FakeFirestoreClient()).scheduler_auth(principal=None, audience=None)
    assert await check("any-token") is False
