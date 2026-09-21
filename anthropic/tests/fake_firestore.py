"""In-memory stand-in for ``firestore.AsyncClient``.

Covers exactly what ``FirestorePendingStore`` uses: ``create`` (raising
``AlreadyExists``), ``set(merge=True)`` with a deep merge and array
transforms, ``update`` under a ``last_update_time`` precondition, and a
query with filters, an order and a limit. The emulator would cover more,
but it is not installed in this environment or in CI, and the sibling
store made the same call — see ``vertex/tests/test_firestore_checkpoint.py``.

Two real behaviours are mimicked deliberately, because a fake that got
them wrong would hide a bug rather than surface one:

- ``FieldFilter(f, "==", None)`` normalizes to an ``IS_NULL`` operator,
  and a document without the field does NOT match it.
- ``update`` raises ``NotFound`` on a missing document and
  ``FailedPrecondition`` when the ``last_update_time`` no longer matches.

``on_get`` is the one thing here the real client has no analogue for: a
callback fired after a snapshot is taken, so a single-threaded test can
slip a competing writer in between a read and the compare-and-set that
follows it. Without it the CAS is unreachable from a sequential test —
the lease guard answers first — and the precondition above would be
mimicked but never exercised.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

from google.api_core.exceptions import AlreadyExists, FailedPrecondition, NotFound
from google.cloud.firestore_v1 import AsyncClient
from google.cloud.firestore_v1.transforms import ArrayRemove, ArrayUnion


class FakeSnapshot:
    def __init__(self, doc_id: str, data: dict[str, Any] | None, update_time: int | None) -> None:
        self.id = doc_id
        self.exists = data is not None
        self.update_time = update_time
        self._data = data

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDocumentReference:
    def __init__(self, client: FakeFirestore, path: str, doc_id: str) -> None:
        self._client, self._path, self.id = client, path, doc_id

    async def get(self) -> FakeSnapshot:
        held = self._client.docs.get(self._path)
        snapshot = (
            FakeSnapshot(self.id, None, None)
            if held is None
            else FakeSnapshot(self.id, held[0], held[1])
        )
        if self._client.on_get is not None:
            # Taken already, so a writer that runs now leaves this caller
            # holding a stale one — which is the race `claim` has to lose.
            await self._client.on_get(self._path)
        return snapshot

    async def create(self, data: dict[str, Any]) -> None:
        if self._path in self._client.docs:
            raise AlreadyExists(self._path)
        self._client.write(self._path, copy.deepcopy(data))

    async def set(self, data: dict[str, Any], merge: bool = False) -> None:
        held = self._client.docs.get(self._path)
        base: dict[str, Any] = copy.deepcopy(held[0]) if (merge and held is not None) else {}
        self._client.write(self._path, _merge(base, copy.deepcopy(data)))

    async def update(self, data: dict[str, Any], option: Any = None) -> None:
        held = self._client.docs.get(self._path)
        if held is None:
            raise NotFound(self._path)
        if option is not None and getattr(option, "_last_update_time", None) != held[1]:
            raise FailedPrecondition(f"stale precondition on {self._path}")
        self._client.write(self._path, _merge(copy.deepcopy(held[0]), copy.deepcopy(data)))


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, ArrayRemove):
            base[key] = [x for x in base.get(key) or [] if x not in value.values]
        elif isinstance(value, ArrayUnion):
            held = list(base.get(key) or [])
            base[key] = held + [x for x in value.values if x not in held]
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(base[key], value)
        else:
            base[key] = value
    return base


class FakeQuery:
    def __init__(
        self,
        collection: FakeCollectionReference,
        predicates: list[tuple[str, Any, Any]] | None = None,
        order: str | None = None,
        bound: int | None = None,
    ) -> None:
        self._collection = collection
        self._predicates = predicates or []
        self._order, self._bound = order, bound

    def where(self, *, filter: Any) -> FakeQuery:
        # The real client's keyword is `filter`; shadowing the builtin here
        # is what makes the call sites identical.
        predicate = (filter.field_path, filter.op_string, filter.value)
        return FakeQuery(self._collection, [*self._predicates, predicate], self._order, self._bound)

    def order_by(self, field_path: str) -> FakeQuery:
        return FakeQuery(self._collection, self._predicates, field_path, self._bound)

    def limit(self, count: int) -> FakeQuery:
        return FakeQuery(self._collection, self._predicates, self._order, count)

    async def stream(self):  # -> AsyncIterator[FakeSnapshot]
        prefix = self._collection.prefix
        rows = [
            (path.removeprefix(prefix), held[0], held[1])
            for path, held in self._collection.client.docs.items()
            if path.startswith(prefix)
        ]
        for field_path, op, value in self._predicates:
            rows = [row for row in rows if _matches(_field_value(row[1], field_path), op, value)]
        if self._order:
            rows.sort(key=lambda row: row[1][self._order])
        for row in rows[: self._bound] if self._bound else rows:
            yield FakeSnapshot(row[0], row[1], row[2])


def _field_value(data: dict[str, Any], path: str) -> Any:
    """``event.conversation_id`` the way Firestore reads it: a dotted path
    walks into nested maps rather than naming a key with dots in it."""
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _matches(actual: Any, op: Any, value: Any) -> bool:
    name = getattr(op, "name", op)
    if name == "IS_NULL":
        # Firestore: a document missing the field does not match.
        return actual is None
    if name == "==":
        return actual == value
    if name == "<=":
        return actual is not None and actual <= value
    raise AssertionError(f"fake does not implement operator {name!r}")


class FakeCollectionReference:
    def __init__(self, client: FakeFirestore, name: str) -> None:
        self.client, self.prefix = client, f"{name}/"

    def document(self, doc_id: str) -> FakeDocumentReference:
        return FakeDocumentReference(self.client, f"{self.prefix}{doc_id}", doc_id)

    def where(self, *, filter: Any) -> FakeQuery:
        return FakeQuery(self).where(filter=filter)


class FakeFirestore:
    """``docs`` maps ``"<collection>/<id>"`` to ``(data, update_time)``."""

    # Static on the real client too, so the store's call site is identical.
    write_option = staticmethod(AsyncClient.write_option)

    def __init__(self) -> None:
        self.docs: dict[str, tuple[dict[str, Any], int]] = {}
        # Set by a test to interleave a competing writer; see the docstring.
        self.on_get: Callable[[str], Awaitable[None]] | None = None
        self._clock = 0

    def write(self, path: str, data: dict[str, Any]) -> None:
        self._clock += 1
        self.docs[path] = (data, self._clock)

    def collection(self, name: str) -> FakeCollectionReference:
        return FakeCollectionReference(self, name)
