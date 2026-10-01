# Input scope and round hashes

**Date:** 2026-09-30
**Status:** Design, ready for planning.
**Target repo:** `slashid-ai-forwarders`, `shared/` (`events.py`, `normalize/`), then each forwarder's call site.
**Companion change:** two optional `AIInvocationObservedV1` fields in `ng-evangelion`, batched into the end-of-Phase-2 schema sync (the server ignores unknown fields until then).

## Problem

`AIInvocationObservedV1.input` is the whole conversation before the response, so a session of R rounds hashes, and with `SLASHID_INCLUDE_RAW_CONTENT` stores, O(R²) bytes. The same event already attributes `used_tools` and `accessed_files` to the round the model consumed (`after_last_assistant`), so `input` is the one field that is not about that round.

Nothing ties the events of one conversation together except `conversation_id`, which several sources lack or share across sub-conversations (every Anthropic hook sub-conversation has the session's `session_id`).

## Overview

Two independent, shared-builder changes that every source gets at once:

1. **Input scope.** `SLASHID_INPUT_SCOPE=session|round` (default `round`). `input` becomes the messages alone, no longer the whole `NormalizedInvocationInput`, and `round` limits it to the round the model consumed.
2. **Round hashes.** Every event carries the hash of its own round and of the previous rounds, so a consumer can stitch events into a conversation without `conversation_id`. Always on, independent of the scope.

## Rounds

A **response** is a maximal run of consecutive assistant messages, merged as the Anthropic hook already merges them. Round k is `(I[k], O[k])`: `O[k]` is response k and `I[k]` is every non-assistant message between response k-1 and `O[k]`. This is the round `after_last_assistant` already selects, so `I[k]` is exactly what `used_tools` and `accessed_files` come from.

## Input scope

| Scope | `input` |
| --- | --- |
| `session` | the whole transcript before the response |
| `round` | `I[k]` |

`input` is the canonical JSON array of the messages, not an object with a `messages` key. Today the builder serializes the whole `NormalizedInvocationInput`, so `tools_declared` and `tool_servers` also feed `input.content_hashes`, although `available_tools` and `available_tool_servers` already carry them. They leave the hash in both scopes, so `input` no longer changes when only the tool list does. A system prompt is a `system` message inside `messages`, always at index 0 (the Anthropic, Converse and Gemini normalizers put it there), so in `round` scope it appears in round 1 only. Claude Code's own API requests also carry `system` messages mid-array (hook output, re-injected file reads; seen in a captured request, not in any hook frame fixture), which stay with their round. The slice is `after_last_assistant(messages)`, so it is stateless and correct for sub-conversations that share a session.

The server does nothing with `input` today, so `round` is the default and `session` is the opt-in for a consumer that wants the whole transcript per event. Both the scope and dropping the tools change `input.content_hashes` for every existing consumer, so they ship together, announced.

## Round hash

```
round_hash[k] = sha256(canonical_json(project(I[k] + O[k])))
```

`I[k] + O[k]` is one message list. It needs no input/output wrapper: `I[k]` has no assistant messages and `O[k]` has only assistant messages, so roles already mark the boundary. Canonical JSON is the one `_build_content` uses (`sort_keys`, compact separators). Hex digest.

### `project`

The same round is seen twice, as the response of event k and as history in event k+1's request, and both must hash alike. A response and its replay are not always identical, so `project` maps `list[NormalizedMessage]` to a reduced form holding only what survives replay:

| Message or block | Projected as |
| --- | --- |
| `system` message | dropped |
| other message | `{"role", "content"}` |
| `text` | `{"kind", "text"}` |
| `tool_use` | `{"kind", "tool_use_id", "tool_name", "tool_input"}` |
| `tool_result` | `{"kind", "tool_use_id", "tool_output", "tool_is_error"}` |
| `image`, `audio`, `document` | `{"kind": "attachment"}`: presence only, no name, `media_type`, `byte_length` or digest |
| `reasoning` | dropped |

Attachments are a bare marker because the same file can arrive as a different kind on another path, `byte_length` differs between extracted text and raw bytes, and digests are enriched after the event is first pushed. An attachment delivered as extracted `text` cannot be coalesced and is the case live capture must size.

Adjacent messages of the same role are merged after projection. A response run can arrive as one merged message in the event and as several in the next request's history, and a user turn can be split across messages; the merge makes both hash alike. A message left empty by the projection is dropped.

It is validated against live traffic per source before the schema is closed. Until a source is validated its events carry the fields but stitch only if a capture shows the hashes match.

## Wire fields

| Field | Content |
| --- | --- |
| `round_hash` | `round_hash[k]`. Absent when the event has no `O[k]`: an enforced denial, or an input-only tail or flushed record. |
| `recent_round_hashes` | `[round_hash[k], round_hash[k-1], …]`, newest first, at most N, only complete rounds. An event without `O[k]` lists the N complete rounds before it. Ends with one marker: `"start"` when its oldest entry is the conversation's first round, `"..."` when older rounds may exist beyond it. |

The markers are literals, not digests, so they cannot collide with a real hash. One is appended after the oldest hash, so a list holds at most N hashes plus a marker. `"start"` tells a conversation that is shorter than N apart from one the producer could only see the last N rounds of, which end in `"..."`. An event with no complete round and no response (an audit-log-only event, a first-turn tail) carries neither field. A producer emits `"start"` only when its transcript demonstrably starts at the beginning of the conversation; a source with windowed history (Codex after a watermark, a reader that caps what it sees) ends its list with `"..."` even when it reaches the start of its window.

`SLASHID_ROUND_LINK_DEPTH` sets N, default **10**. Only the last N rounds, and one more to decide the marker, are projected and hashed, so the cost is O(N) rounds per event even in `session` scope.

## Stitching (server side)

Two events are the same conversation if their `recent_round_hashes` share at least M real hashes, **M = 4** by default, counting the markers for nothing: every fresh conversation ends in `"start"`. If the shorter list ends in `"start"` it is the whole conversation so far, and the requirement drops to `min(M, its real hashes)`. M is a server rule; producers only guarantee N.

- Events d rounds apart share N-d entries, so the events on either side of g lost ones share N-g-1, and with N=10 and M=4 a chain survives 5 consecutive lost events.
- The relaxed rule applies only with `"start"`, so the first M-1 events of a session stitch, while a short list from a source that merely sees little history still needs M.
- A single matching hash is weak evidence: identical short rounds ("continue" → same reply) collide across sessions. Four in a row make that practically impossible, which is why M is not 1.
- Local, not cumulative, hashes recover from history rewrites. After Claude Code compacts a context, events match again M rounds later.

## Per-source notes

- **Bedrock, Vertex.** The request carries the whole history, so rounds come from `NormalizedInvocation.input.messages`, and `O[k]` from the response. Both are built in `build_event_from_normalized`.
- **Anthropic hook.** Event k names the previous run, so `input` already ends before it. `I[k]` is the last round of `split.before` and `O[k]` the trailing run. Tail records and denials have no `O` (above); their `recent_round_hashes` lists the complete rounds in the frame, which includes the trailing run's round, so a tail stitches to the event emitted beside it. Compliance-reader events are rebuilt from truncated tool blocks and a synthetic system message, and match only other reader events.
- **Codex.** Rounds are delimited by `token_usage_record`s in the rollout, `O[k]` is the response whose `response_id` is the `request_id`. The collection-owned history cache already holds the last rounds. This replaces the "make `input` configurable" follow-up in the Codex spec.

## Known limitations

- A hook-built and a reader-built round of the same conversation hash differently, and do not stitch.
- A round's hash is as guessable as its content; for short rounds it is no less reversible than the existing `input` hash.
- `session` scope keeps the quadratic input cost, and is opt-in.
- A transcript ending on an assistant message is read as ending on the event's response, so a prefill request that errored claims its last history round as its own `round_hash`.
- `"start"` marks where the visible transcript starts. A client that trims its own context window makes a later event look like a conversation start, and so does a compaction.
- **Claude Code compaction** (one `claude -p` capture, `/compact` on a five-message session, behind a logging proxy). The summarising call is itself a model call: the unchanged history, with the compaction instruction appended to the last user message as an extra text block, so its round differs from the one that already answered that message. The next request holds none of the earlier rounds: a user message with the summary, the last assistant message kept verbatim, and synthetic `system` messages re-injecting recent file reads. Its only complete round is `(summary → kept assistant message)`, so its list is that hash plus `start`, and nothing stitches across the compaction. Not verified: whether a larger session keeps more than the last message, or whether interactive and auto compaction differ.

## Tests

- Rounds: merged assistant runs are one response; a transcript without an assistant message is one round; `system` messages are ignored.
- Projection: a response and the same message replayed inside the next request hash equal, including a reasoning block present only on the response and a response run split into several messages in the replay.
- Scope: `round` equals `after_last_assistant`; `input` hashes the messages only, so changing `tools_declared` leaves it unchanged; a leading `system` message is in round 1 and absent from round 2.
- Fields: first event lists one hash and `start`; the 10th lists ten hashes and `start`, the 11th ten and `...`; the 11th event lists ten; consecutive events share N-1 entries; denial and tail records omit `round_hash` and still list prior rounds.
- Stitch property test: up to N-M-1 consecutive events dropped from a stream, the survivors on either side still stitch.
- Live capture per source before closing the projection.
