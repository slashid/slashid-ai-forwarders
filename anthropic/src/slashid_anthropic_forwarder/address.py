"""Addresses — the keys a pending record can be filed under.

Only a model-minted ``tool_use.id`` agrees across the two sources, and
that was measured rather than reasoned: over one session present in both
the captured frames and the stored transcript, a digest over the
transcript prefix through each assistant run produced 200 keys on the
frame side, 302 on the reader side and **zero in common** — still zero
after dropping synthetic markers, still zero with every text block
removed. The stored transcript is a different projection of the
conversation (a prepended synthetic marker, turns from before capture
began, sub-agent turns the frame never shows), so no function of the
message sequence can survive the crossing. An opaque token the model
minted once can, and does.

That is a measurement of today, not a rule. **Joinability is a seam.**
A provider-supplied invocation id carried by both surfaces is the
obvious fix and the README asks for it, so every rule downstream is
stated in terms of whether a run **can be addressed** — never whether it
contains a tool call. ``joinable_address`` is the one place that knows
the difference: when a common id ships it becomes the preferred anchor
ahead of the ``toolu_`` one, that function stops returning ``None``, and
what a reader may emit, what it may enrich and which runs get a tail all
widen by themselves. Nothing else may test for a tool call.

So there are four address spaces, and which one a record gets decides
who may write it:

- ``joinable_address`` — both sources compute it; either may open the
  record and the other may complete it.
- ``hook_address`` — the hook alone, for a run ``joinable_address``
  cannot address yet. No reader can compute a delivery id, and that is
  the point: emitting the same invocation under a second key would
  double-count it, since the terminal's dedup is first-completed-wins
  and never merges.
- ``tail_address`` — the hook alone, for the fresh round a frame carries
  that no successor may ever report. Hook-local, so it may use an
  encoding the cross-source key cannot.
- ``deny_address`` — the round whose deny was honoured, keyed on the
  delivery the activity names so Reader A can compute it. It is a fourth
  space rather than a second use of ``hook:`` because one frame can carry
  an unjoinable previous run *and* an honoured deny on its fresh round,
  and one key for both would merge two unrelated invocations into one
  record.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicRequestMessage,
    AnthropicTextBlock,
    AnthropicToolResultBlock,
    AnthropicToolUseBlock,
)

# How much of a text block contributes to a tail digest. A Claude Code
# transcript reaches 1.86 MB and a frame arrives every round, so the digest
# has to be cheap; a 256-character prefix per block, together with the block
# count, the roles and every tool id, was exact over the measured corpus —
# 239 distinct keys from 284 deliveries, zero false merges, zero false
# splits against the toolu_ ids as ground truth.
TEXT_PREFIX_CHARS = 256

_TAIL_VERSION = b"tail/1"


def joinable_address(run: Sequence[AnthropicRequestMessage]) -> str | None:
    """The first ``tool_use.id`` in a completed assistant run, or ``None``.

    ``run`` is one response, which may have arrived as several consecutive
    assistant messages; block order across them decides. No ordinal enters
    the key: one ``session_id`` carries a hundred-plus sub-conversations,
    so any per-session counter collides.

    ``None`` means "no shared address **yet**", not a category: this
    function is the seam. 194 of 284 measured trailing runs had a tool
    call, but only 6 of 14 on claude.ai — and callers ask this rather
    than testing for a tool call themselves, so when the provider ships
    an id both surfaces carry it is preferred here ahead of the
    ``toolu_`` one, ``None`` stops happening, and every rule downstream
    widens with no other edit.
    """
    for message in run:
        for block in message.content:
            if isinstance(block, AnthropicToolUseBlock):
                return block.id
    return None


def hook_address(webhook_id: str) -> str:
    """The address of a run the hook alone can see: ``hook:`` + the delivery id.

    Unique without agreeing with anything, because nothing else has to
    compute it. A reader that finds an unjoinable run leaves it alone.
    """
    return f"hook:{webhook_id}"


def deny_address(webhook_id: str) -> str:
    """The address of a round whose deny was honoured: ``deny:`` + the delivery id.

    Keyed on the delivery, which is what the denial activity names, but
    under its own prefix. One frame can carry an unjoinable previous run
    *and* an honoured deny on its fresh round — two invocations, and
    ``hook:`` for both would merge them into one record.
    """
    return f"deny:{webhook_id}"


def tail_address(transcript: Sequence[AnthropicRequestMessage], session_id: str | None) -> str:
    """``tail:`` + a digest over a frame's WHOLE transcript.

    Hook-local: no reader ever computes it, so it may use an encoding that
    the cross-source key provably cannot. Intra-source it was exact — 239
    distinct keys from 284 deliveries, zero false merges and zero false
    splits against the toolu_ ids as ground truth.

    It must be **reconstructible**, because the successor frame's whole job
    is to discard the record: frame N+1 drops its own trailing assistant run
    and the round after it — ``split_transcript(...).before`` — and calls
    this with the result. The canonical encoding is therefore part of the
    contract, spelled out in ``_canonical_bytes``.
    """
    return "tail:" + hashlib.sha256(_canonical_bytes(transcript, session_id)).hexdigest()


def _canonical_bytes(
    transcript: Sequence[AnthropicRequestMessage], session_id: str | None
) -> bytes:
    """The byte stream a tail digest is taken over.

    Every field is length-prefixed (4-byte big-endian) so content cannot
    forge a separator. Per message: the role, then the number of blocks that
    contributed, then each contributing block's fields. ``thinking`` and
    unknown block types contribute nothing — frames carry no thinking blocks
    today, and a block type invented next quarter must not move an existing
    key. The version tag leads, so a future encoding change is a new key
    space rather than a silent re-addressing of live records.
    """
    out = [
        _field(_TAIL_VERSION),
        _field(_prefix(session_id)),
        _field(str(len(transcript)).encode()),
    ]
    for msg in transcript:
        emitted = [f for f in (_block_fields(b) for b in msg.content) if f is not None]
        out.append(_field(msg.role.encode("utf-8")))
        out.append(_field(str(len(emitted)).encode()))
        for fields in emitted:
            out.extend(_field(f) for f in fields)
    return b"".join(out)


def _field(raw: bytes) -> bytes:
    return len(raw).to_bytes(4, "big") + raw


def _prefix(text: str | None) -> bytes:
    """NFC-normalize, THEN truncate. The other order can split a combining
    sequence and change what normalization produces."""
    if not text:
        return b""
    return unicodedata.normalize("NFC", text)[:TEXT_PREFIX_CHARS].encode("utf-8")


def _block_fields(block: object) -> list[bytes] | None:
    """The fields one content block contributes, or ``None`` to skip it."""
    if isinstance(block, AnthropicTextBlock):
        return [b"t", _prefix(block.text)]
    if isinstance(block, AnthropicToolUseBlock):
        return [b"u", block.id.encode("utf-8"), _prefix(block.name)]
    if isinstance(block, AnthropicToolResultBlock):
        # The result's content is not hashed: it is capped at 10 KB on the
        # reader's side and untruncated here, and the pairing id already
        # identifies it uniquely.
        return [b"r", block.tool_use_id.encode("utf-8"), b"1" if block.is_error else b"0"]
    if isinstance(block, AnthropicAttachmentBlock):
        return [b"a", _prefix(block.file_name), _prefix(block.text)]
    return None
