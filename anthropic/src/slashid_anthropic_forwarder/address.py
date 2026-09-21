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
who may write it. Three of them are here; the fourth belongs to Reader A
and lands with it:

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
- ``deny_address`` — **not here**: Reader A's, added in the chunk that
  builds it. It is a fourth space rather than a second use of ``hook:``
  because one frame can carry an unjoinable previous run *and* an
  honoured deny on its fresh round, and one key for both would merge two
  unrelated invocations into one record.
"""

from __future__ import annotations

from collections.abc import Sequence

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicRequestMessage,
    AnthropicToolUseBlock,
)


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
