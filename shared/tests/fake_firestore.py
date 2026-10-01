"""An in-memory ``firestore.AsyncClient`` for the platform tests.

Documents carry a version, so ``update`` can honour the precondition the tick
lease depends on.
"""

from __future__ import annotations

from typing import Any

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound


class _Option:
    def __init__(self, last_update_time: int) -> None:
        self.last_update_time = last_update_time


class _Doc:
    def __init__(self, db: FakeFirestore, key: str) -> None:
        self._db, self._key = db, key

    async def get(self) -> Any:
        held = self._db.docs.get(self._key)
        data, version = held if held else (None, None)
        fields = {
            "exists": held is not None,
            "update_time": version,
            "to_dict": lambda _: None if data is None else dict(data),
        }
        return type("Snap", (), fields)()

    async def create(self, data: dict[str, Any]) -> None:
        if self._key in self._db.docs:
            raise AlreadyExists(self._key)
        self._db.write(self._key, dict(data))

    async def set(self, data: dict[str, Any], merge: bool = False) -> None:
        held = self._db.docs.get(self._key)
        self._db.write(self._key, {**held[0], **data} if merge and held else dict(data))

    async def update(self, data: dict[str, Any], option: _Option | None = None) -> None:
        held = self._db.docs.get(self._key)
        if held is None:
            raise NotFound(self._key)
        if option is not None and option.last_update_time != held[1]:
            raise FailedPrecondition(self._key)
        self._db.write(self._key, {**held[0], **data})


class FakeFirestore:
    def __init__(self) -> None:
        self.docs: dict[str, tuple[dict[str, Any], int]] = {}
        self._clock = 0

    def write(self, key: str, data: dict[str, Any]) -> None:
        self._clock += 1
        self.docs[key] = (data, self._clock)

    def collection(self, name: str) -> Any:
        return type("Col", (), {"document": lambda _, doc: _Doc(self, f"{name}/{doc}")})()

    @staticmethod
    def write_option(*, last_update_time: int) -> _Option:
        return _Option(last_update_time)
