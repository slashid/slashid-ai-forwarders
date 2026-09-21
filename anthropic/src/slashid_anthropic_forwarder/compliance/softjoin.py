"""The soft join — attachment digests, and nothing else.

The ``toolu_`` anchor addresses two rounds in three and only 1 of 4
attachment-bearing ones, so the digests a reader can supply mostly have
nowhere to go. This is the second, weaker join that carries them there,
and it is weaker in exactly one dimension:

* the **conversation** half is not soft at all. A frame's ``session_id``
  is the chat's own identifier, the one its ``href`` ends with - three
  of three, measured - so candidates scope to one conversation exactly.
* the **time** half is a judgement call, so it is made unanimously.
  Within the window, one candidate enriches and any other number
  abstains. Nearness was measured and is not enough: the nearest record
  sat 0.3 to 6.9 s away with the runner-up at least 6.1 s further, yet
  one chat has three frames inside 15 s of two messages, where
  nearest-wins picks confidently and wrongly.

**A soft match may enrich and may never emit**, and that is the whole
reason it is safe: a wrong match costs wrong hashes on one event rather
than a duplicated or misattributed invocation, which is the failure the
addressing scheme exists to prevent. The bound is the signature.
``DigestTarget`` is two methods — no ``upsert``, so nothing here can
create a record; no sink and no ``Config``, so nothing here can push
one; no addressing import, so nothing here decides an address. It
writes to the address a candidate already had.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from ..record import COMPLIANCE_SOFT, Append, PendingRecord
from ..store import Outcome

log = logging.getLogger(__name__)


class DigestTarget(Protocol):
    """The two operations a soft match may reach for, and no others.

    ``PendingStore`` satisfies it, so the caller hands over the real
    store and the callee still cannot reach past these.
    """

    async def nearby(
        self,
        conversation_id: str,
        *,
        at: datetime,
        window: timedelta,
        limit: int = 25,
    ) -> list[PendingRecord]: ...

    async def complete(
        self,
        address: str,
        fields: dict[str, Any],
        clears: Sequence[str] = (),
        *,
        now: datetime | None = None,
    ) -> Outcome: ...


class SoftMatch(StrEnum):
    ENRICHED = "enriched"
    # Several candidates: the ambiguity the unanimity rule exists for.
    AMBIGUOUS = "ambiguous"
    # None at all — the hook never recorded this conversation, or the
    # records it did are already pushed.
    NONE = "none"


async def soft_join(
    target: DigestTarget,
    *,
    conversation_id: str,
    at: datetime,
    digests: Sequence[Mapping[str, Any]],
    window: timedelta,
) -> SoftMatch:
    """Deliver ``digests`` to the one record they can only belong to.

    Nothing is cleared: the expectation set is the hard join's business,
    and a record waiting on ``file_digests`` has an address the reader
    reaches directly. The ``Outcome`` is deliberately dropped —
    readiness is a push decision, and this function has nothing to push
    with.
    """
    if not digests or not conversation_id:
        return SoftMatch.NONE
    candidates = await target.nearby(conversation_id, at=at, window=window)
    if len(candidates) != 1:
        log.info(
            "soft join: %d candidates in %s within %ss; abstaining",
            len(candidates),
            conversation_id,
            int(window.total_seconds()),
        )
        return SoftMatch.AMBIGUOUS if candidates else SoftMatch.NONE
    await target.complete(
        candidates[0].address,
        {
            "file_digests": [dict(d) for d in digests],
            # Its own value, not COMPLIANCE: the record then says which
            # kind of join supplied these hashes, which is the one thing
            # a reviewer needs in order to weigh them.
            "contributed": Append((COMPLIANCE_SOFT,)),
        },
        (),
    )
    return SoftMatch.ENRICHED
