"""The pending record: a partial event plus the control envelope around it.

The event is stored as a serialized mapping, not a validated model.
``AIInvocationObservedV1`` requires ``parsed_as``, which depends on which
sources end up contributing and is unknowable while the record is
pending, and its base sets ``extra="forbid"``, so the envelope cannot
ride inside it. Validation happens at push, in ``to_event`` — the one
path that can log and retry.

The 1 MiB document bound is enforced here rather than in the storage
adapter: the adapter should write what it is handed, and deciding what
to drop is a statement about the event's content. Dropping raw text sets
a marker so the absence reads as elision rather than as an invocation
that carried none.

There is no ``to_document``: nothing ever writes a whole record. A
writer supplies fields, the store merges them, and ``from_document``
reads the result back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from slashid_ai_forwarder_core.events import AIInvocationObservedV1

# Firestore's own document limit is 1 MiB including field names and
# indexing overhead; the event is bounded below it and the envelope is a
# few hundred bytes.
MAX_EVENT_BYTES = 1024 * 1024
ELISION = "[slashid: elided — the pending record exceeded 1 MiB]"

# `contributed` values: which source actually supplied a field, which is
# not the same as which visited.
HOOK = "hook"
COMPLIANCE = "compliance"

# The two members the expectation set can hold, both cleared by a reader.
# A frame-built record is complete on arrival except for attachment byte
# digests, so the common case waits for nothing; a denial has no successor
# frame at all, so it waits for Reader A wherever one exists.
FILE_DIGESTS = "file_digests"
DENIAL_ACTIVITY = "denial_activity"

PARSED_AS_HOOK = "anthropic-inference-hook"
PARSED_AS_COMPLIANCE = "anthropic-compliance"
PARSED_AS_JOINED = "anthropic-joined"


@dataclass(frozen=True)
class Append:
    """Append-to-set marker for a field two writers may both extend.

    Backend-neutral on purpose: the adapter translates it into Firestore's
    array transform, so no sentinel from a vendor SDK reaches this module
    or the ``PendingStore`` protocol.
    """

    values: tuple[str, ...]


@dataclass(frozen=True)
class PendingRecord:
    """One pending invocation, as the store holds it right now.

    ``event`` is the serialized partial ``AIInvocationObservedV1``. The
    rest is the control envelope, which never reaches the wire.
    """

    address: str
    event: dict[str, Any]
    deadline: datetime
    # When the sweep may look at this record again: the deadline on
    # creation, the end of the lease while claimed, the end of the backoff
    # after a failed push. One field, so "claim absent or expired" is a
    # single inequality rather than a second one the index has to carry.
    next_attempt_at: datetime
    webhook_ids: list[str] = field(default_factory=list)
    verdict: str | None = None
    composed_verdict: str | None = None
    awaiting: list[str] = field(default_factory=list)
    contributed: list[str] = field(default_factory=list)
    attempts: int = 0
    claim_owner: str | None = None
    claim_expires_at: datetime | None = None
    tombstoned_at: datetime | None = None
    elided: bool = False

    @property
    def ready(self) -> bool:
        """Readiness is a state, not a transition. A record born ready — under
        emit-previous, most of them — has no transition at all, so asking
        "did this call empty the expectation set?" cannot arbitrate."""
        return self.tombstoned_at is None and not self.awaiting


def event_fields(event: AIInvocationObservedV1) -> dict[str, Any]:
    """Serialize a partial event into merge fields, bounded at 1 MiB."""
    body, elided = _bound(event.model_dump(mode="json", exclude_none=True))
    fields: dict[str, Any] = {"event": body}
    if elided:
        # Only ever set, never cleared: a later merge of a small field must
        # not make an elided record look intact.
        fields["elided"] = True
    return fields


def open_fields(
    event: AIInvocationObservedV1,
    *,
    webhook_id: str,
    contributed: str,
    verdict: str | None = None,
    composed_verdict: str | None = None,
) -> dict[str, Any]:
    """The fields that open a record — or merge into one already open.

    ``webhook_ids`` appends rather than replaces: 43 of 239 measured
    invocations were revealed by more than one delivery, and Reader A
    matches a denial against any of them. A ``None`` verdict is omitted
    rather than merged, so a reader-opened record cannot erase the verdict
    a frame supplied.
    """
    fields = event_fields(event)
    fields["webhook_ids"] = Append((webhook_id,))
    fields["contributed"] = Append((contributed,))
    if verdict is not None:
        fields["verdict"] = verdict
    if composed_verdict is not None:
        fields["composed_verdict"] = composed_verdict
    return fields


def parsed_as(contributed: list[str]) -> str:
    """``anthropic-joined`` only when more than one source contributed."""
    sources = set(contributed)
    if len(sources) > 1:
        return PARSED_AS_JOINED
    if sources == {COMPLIANCE}:
        return PARSED_AS_COMPLIANCE
    return PARSED_AS_HOOK


def to_event(record: PendingRecord) -> AIInvocationObservedV1:
    """Validate a record into the event that goes on the wire.

    Raises ``ValidationError`` (a ``ValueError``) on a record that never
    became a whole event — which is why this runs at push, where the
    caller can log it and retry, and not on the request path.
    """
    return AIInvocationObservedV1.model_validate(
        {**record.event, "parsed_as": parsed_as(record.contributed)}
    )


def from_document(address: str, data: dict[str, Any]) -> PendingRecord:
    return PendingRecord(
        address=address,
        event=data.get("event") or {},
        deadline=data["deadline"],
        next_attempt_at=data["next_attempt_at"],
        webhook_ids=list(data.get("webhook_ids") or []),
        verdict=data.get("verdict"),
        composed_verdict=data.get("composed_verdict"),
        awaiting=list(data.get("awaiting") or []),
        contributed=list(data.get("contributed") or []),
        attempts=int(data.get("attempts") or 0),
        claim_owner=data.get("claim_owner"),
        claim_expires_at=data.get("claim_expires_at"),
        tombstoned_at=data.get("tombstoned_at"),
        elided=bool(data.get("elided")),
    )


def _sizeof(body: dict[str, Any]) -> int:
    return len(json.dumps(body, separators=(",", ":")).encode())


def _bound(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Drop raw text until the event fits, largest field first.

    The size check runs before any copying, so the common case — 475 of 492
    measured frames — pays one serialization and nothing else. Never fails:
    the last step drops the file and tool lists outright, leaving identity,
    hashes and the envelope, which cannot approach the bound.
    """
    if _sizeof(body) <= MAX_EVENT_BYTES:
        return body, False
    body = json.loads(json.dumps(body))  # detach: the caller's event stays whole
    elided = False
    for step in (_elide_input, _elide_output, _elide_files, _drop_lists):
        elided = step(body) or elided
        if _sizeof(body) <= MAX_EVENT_BYTES:
            break
    return body, elided


def _elide_input(body: dict[str, Any]) -> bool:
    # The input is the whole transcript — 1.86 MB at the measured maximum —
    # so it goes first.
    return _elide_text(body.get("input"), "redacted_text")


def _elide_output(body: dict[str, Any]) -> bool:
    return _elide_text(body.get("output"), "redacted_text")


def _elide_files(body: dict[str, Any]) -> bool:
    return any(
        [_elide_text(entry, "redacted_content") for entry in body.get("accessed_files") or []]
    )


def _drop_lists(body: dict[str, Any]) -> bool:
    body.pop("accessed_files", None)
    body.pop("used_tools", None)
    return True


def _elide_text(holder: Any, key: str) -> bool:
    if isinstance(holder, dict) and holder.get(key) is not None:
        holder[key] = ELISION
        return True
    return False
