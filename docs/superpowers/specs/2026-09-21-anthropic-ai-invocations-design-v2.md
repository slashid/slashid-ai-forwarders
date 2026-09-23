# Anthropic AI-invocation collection — inference hooks and compliance pull

**Date:** 2026-09-21
**Status:** Design, ready for planning. Every wire claim below was measured against a live Claude Enterprise tenant on 2026-09-20/21; nothing here is inferred from documentation alone.
**Supersedes:** `2026-09-18-anthropic-inference-hooks-design.md`, which described the hook in isolation.
**Target repo:** `slashid-ai-forwarders`, new subdirectory `anthropic/`, beside `bedrock/`, `vertex/` and `shared/`.
**Companion changes:** `POST /ip/nhi/events/ai-invocations/preflight` in `ng-evangelion` ([#7733](https://github.com/slashid/ng-evangelion/pull/7733)); a batched schema sync for `AIAccessedFile.provenance` and `AnthropicIdentityDetails`.

## Overview

One component that collects `AIInvocationObservedV1` events for Claude Enterprise activity — Claude Code, Cowork and claude.ai — from **two independent sources**, and enforces inline where the protocol allows it.

```
                    ┌──────────────── push ────────────────┐
 user prompt ──► Anthropic ──signed POST──► receiver ──► verdict (allow/deny, <5 s)
                                              │
                                              ▼
                                      pending invocation ──► SlashID NHI
                                              ▲
                    ┌──────────────── pull ───┘
 Activity Feed ─────┤  denials, configuration
 Transcripts   ─────┤  responses, final turns, attachment bytes
```

The **hook** is the only surface on any provider that can stop a request before the model sees it, and it carries untruncated content. The **compliance surfaces** see everything the hook cannot: the final turn of a session, attachment bytes, and the authoritative record of what was actually blocked. Neither dominates, so both exist and either can run alone.

## Why this surface

Neither Anthropic nor OpenAI exposes per-invocation content for API-key workloads, and their post-hoc surfaces cannot block.

| Surface | Mechanism | Content | Real-time | Can block | User identity |
| --- | --- | --- | --- | --- | --- |
| Anthropic Activity Feed | pull | no | <1 min | no | yes |
| Anthropic Compliance transcripts | pull | yes, 10 KB-capped tool blocks | minutes | no | yes |
| **Anthropic Inference hooks** | **push** | **yes, untruncated** | **inline** | **yes** | **yes** |
| OpenAI Compliance Logs Platform | pull | yes | minutes | no | yes |
| Anthropic/OpenAI API-key workloads | — | **none** | — | no | — |

Hooks fire on both the opening prompt and each returning tool result, because a tool result going back into the model is itself an inference call. So a `Read` of a sensitive file is inspected *before its contents reach the model*, which is the file-exfiltration control at the only moment it can be enforced.

## Goals

1. Emit `AIInvocationObservedV1` for Claude Enterprise activity, attributed to a named user, with no software on the endpoint.
2. Deny, inline, any governed request whose newly-arriving content matches a file tagged sensitive in the customer's graph, or that the organization's graph policy denies.
3. Work with either credential alone, and better with both.
4. Reuse `shared/` for normalization, hashing, batching and delivery.
5. Never let a failure in the SlashID event path degrade into a blocked user.

## Non-goals

- **Prompt mutation.** Verdicts are allow or deny; the protocol supports nothing else.
- **Policy evaluation in this component.** Graph policy lives in `ng-evangelion`'s receiver; this forwards to it and composes the answer.
- **Tool and MCP-server inventory.** No surface exposes tool definitions, so "this identity may not use that MCP server" cannot be enforced before the fact here.
- **Agentic sub-task traffic.** claude.ai's extended research task emits no frames at all; only the launching turn and the returned result are governed.
- **Token accounting.** No surface carries per-invocation usage.
- **Bedrock / Vertex-hosted Claude**, covered by the sibling forwarders, and **Claude Console API-key traffic**, which neither surface exposes.

## Prerequisites

**Hook.** Claude Enterprise; `organization:manage` (Owner or Primary owner) to configure it; an `https://` endpoint on port 443, publicly routable, valid public CA certificate, no redirects, no reverse tunnels; a signing secret generated during setup.

**Compliance.** The Compliance API enabled by the **primary owner**, which is not retroactive — nothing before enablement is recorded, ever. A Compliance Access Key (`sk-ant-api01-…`) with `read:compliance_activities` and `read:compliance_user_data`.

**Both.** A SlashID push token for an **`anthropic`** connection. One deployment shares one `Config`, so both sources use that token by construction — the constraint only matters if someone splits them, which the asymmetry invites: the compliance half needs nothing hosted while the hook needs a public endpoint, so running the readers centrally and the receiver in the customer's project is a tempting topology. **Do not.** Two deployments cannot share a pending store, so the join disappears and each half emits independently; content addressing still makes both compute the same `request_id`, so a shared push token leaves the dedup to collapse the overlap and you lose only the enrichment. **Separate connections lose even that**, because the dedup key is `{org}:{conn}:{request_id}`, and every invocation both halves saw is counted twice.

## What the wire actually looks like

Measured, not quoted. These facts drive every design decision below.

### The prompt frame

| Field | Notes |
| --- | --- |
| `type` | `"prompt"` today. **An unrecognized value must be answered `allow`**, never an error. |
| `request_id` | per-delivery id, equal to the `webhook-id` header |
| `tenant_id` | **equals the Compliance API's `organization_uuid`** — and *not* the id the claude.ai settings page displays, which matches nothing in the API and must never be copied into a binding |
| `actor` | `{type: "user", id: "user_01…", email_address}`, both nullable. `actor.id` **equals** the compliance `user.id`. |
| `source.application` | open string: `claude-ai`, `claude-code`, `cowork`, `config-test`. Advisory routing metadata, **not** a trust boundary. |
| `messages` | cumulative transcript, untruncated |
| `session_id` | conversation id; for claude.ai the chat UUID. **Not unique per conversation** — see below. |
| `model` | public model id, nullable |
| `metadata` | empty in all 492 captured frames |

Content blocks are `text`, `tool_use{id, tool_name, input}`, `tool_result{content, is_error, tool_name, tool_use_id}` and `attachment{file_name, media_type, size_bytes, text}`. Unknown block types must be skipped, never rejected. Absent by design: system prompts, tool definitions, thinking blocks, raw bytes, usage.

The hook spells the tool name `tool_name` where the Messages API spells it `name`, so `shared/normalize/anthropic` parses frames with a `validation_alias` rather than a parallel schema, plus one new `attachment` block.

### Measured properties that shape the design

- **Size, and it is bimodal by surface.** Claude Code: median 664 KB, p90 1.64 MB, **max 1.86 MB** (a 561-message transcript). claude.ai: median 3.4 KB, max 14.8 KB. Receipt-to-response was 39–42 ms for the largest, so the verdict budget is entirely outbound calls.
- **`webhook-id` is per delivery, not per invocation.** All 492 deliveries carried a distinct `webhook-id`, but they revealed only **239 invocations**: 284 frames carried a previous assistant run, and **43 invocations were revealed by more than one delivery**, 45 redundant deliveries in all. Keying events on the delivery would double-count 18% of invocations. The cause is structural — a transcript's trailing assistant run stays trailing until the model produces a new one.
- **The content anchor is available for most invocations but not all.** Of the 239, **175 anchor on a `toolu_` id** and 64 fall back to the content hash, so roughly one invocation in four depends on the fallback digest agreeing byte-for-byte across sources. That is the design's single load-bearing assumption and the tests measure it directly rather than asserting it.
- **One `session_id` carries many conversations.** Alongside a main transcript reaching 231 assistant runs, 100-plus one-message frames arrived under the same id: Haiku status summaries, a `web_search` sub-request, deferred-tool probes. Any per-session ordinal collides.
- **Consecutive user messages happen.** Deferred-tool loading appends a separate "Tool loaded." user message after a `tool_result`. Messages a user sends in quick succession are appended as extra *text blocks* to one message, not as new messages.
- **Server tools are visible but unlabelled.** A `web_search` arrives as an ordinary `tool_use`/`tool_result` pair; nothing marks it server-executed, and its result is `[non-text content]` placeholders, so its content is outside any check.
- **Remote Control changes nothing.** Attaching a local session keeps the same `session_id` and transcript; the claude.ai-side id never appears.
- **No enforcement-mode indicator.** Across a 200-frame window spanning both settings — 99 delivered under shadow mode, 101 under blocking — frames are structurally identical: no `is_shadow`, nothing to infer one from.

### The compliance surfaces

- **Session ids are structured.** `clls_` decodes to `{"v":1,"o":<org uuid>,"p":<account uuid>,"s":<session uuid>}`, where **`s` is the frame's `session_id`**. Match by listing sessions and decoding `s`, not by constructing the id: construction works but depends on a versioned encoding and an account UUID no frame carries.
- **`provenance` separates produced turns from replayed history.** It is an object, `{"type": "client_asserted"}`, not a bare string. In the largest local session — 872 messages, 433 of them assistant — 429 were `client_asserted`, 5 `synthetic_marker` and 438 unmarked. **Only newly-produced assistant turns carry a `model`: 4 of 433.** That ratio is the point. A reader that emitted per assistant message would emit a hundred times the real traffic, so the `model` marker, not the role, is the anchor: one invocation per newly-produced assistant turn. It also sidesteps ordinal collisions entirely.
- **`tool_use.id` is identical across sources.** The same `toolu_…` appears in the frame and the transcript, so content addressing converges wherever a run contains a tool call.
- **Tool blocks are capped** at 10,000 bytes by default, ~1 MiB on request, and flagged `truncated`.
- **Attachments carry digests.** A chat message's `files[]` gives `id`, `filename`, `mime_type`, `size_bytes` and **`md5`**, and the md5 equals that of the downloaded bytes. `…/files/{id}/content` streams them; `HEAD` is a 404.
- **Stored bytes are often the originals.** A 59 KB PDF came back as a real PDF, so it *can* match a graph `FileHash` — something no frame could do. A JPEG came back 72,878 B against the frame's declared 70,657, a processed copy. Plain text matched the frame's digest exactly.
- **Denial activities are rich but modelless.** `inference_hooks_request_denied` carries `request_id` (the `webhook-id`), `conversation_id` (the `session_id`), our `reference_id`, `surface`, both organization identifiers, and an `actor` with `user_id` and a **real client user agent** (`claude-cli/2.1.278`) that no frame carries. It has **no `model`**.
- **A deny is recorded only when it was honoured.** Enforced denial → activity. Shadow-mode denial → nothing. The feed is therefore the authoritative record of what was actually blocked.
- **`inference_hooks_config_updated` records the whole configuration** — `enabled`, `shadow_mode`, `fail_mode`, `rollout_percentage`, both verdict timeouts, `enforcement_mode`, `webhook_url`, `extra_header_names` — so the enforcement state at any instant is reconstructible. It also shows `enforcement_mode: "prompt_only"` and a separate `final_verdict_timeout_ms`, meaning Anthropic already models the response-side hook.
- **Our own reads are audited** as `compliance_api_accessed`, so a poller must filter its own noise.
- **No `stop_reason` and no tokens**, anywhere. Confirmed by scanning every captured response.

## Architecture

### Capabilities follow the credentials

There is no mode switch. Each source turns itself on when its credential is present.

| Configured | Capability |
| --- | --- |
| `SLASHID_HOOK_SIGNING_SECRET` | inline verdicts, and eventing from frames |
| `SLASHID_COMPLIANCE_KEY` | periodic reads of activities and transcripts |
| both | both, joined |
| neither | startup fails |

**Hook only** enforces, and emits untruncated events one round behind. **Compliance only** hosts nothing, configures nothing in claude.ai, cannot enforce, and sees every turn including the last. **Both** joins them.

### What can be joined, and what cannot

**Only a model-minted `tool_use.id` agrees across the two sources.** This was measured, not reasoned. Taking one session present in both the captured frames and the stored transcript, a digest over the transcript prefix through each assistant run produced **200 keys on the frame side, 302 on the reader side, and zero in common** — and still zero after dropping `synthetic_marker` messages, and still zero with every text block removed so that only role and block ids contributed. The `toolu_` ids matched.

The cause is structural, not a rounding problem. The stored transcript is a different projection of the conversation: it prepends a synthetic marker the client never sent, it carries turns from before capture began, and it includes sub-agent turns the frame never shows. No function of the message sequence can survive that. A `toolu_` id can, because it is an opaque token the model minted once and both surfaces carry verbatim.

| | Frames | Transcript | Shared |
| --- | --- | --- | --- |
| Transcript-prefix digests | 200 | 302 | 0 |
| `toolu_` anchors | 154 | 167 | 66 |

**This is expected to improve, so joinability is a seam rather than a rule.** A provider-supplied invocation id carried by both surfaces is the obvious fix and the README asks for it. Everything below is therefore written in terms of whether a run **can be addressed**, never in terms of whether it contains a tool call. One function, `joinable_address(run)`, is the only place that knows the difference. The day a common id ships it becomes the preferred anchor ahead of the `toolu_` one, that function stops returning `None`, and every rule downstream — what the reader may emit, what it may enrich, which records get a tail — widens by itself with no other edit. The coverage figures here are a measurement of today, not a property of the design.

So invocations divide into two classes, and the design turns on which one a run is in.

- **Joinable** — the run contains a `tool_use` block. Its address is that first `toolu_` id. Both sources compute it, so a record can be opened by one and completed by the other. Measured: **194 of 284 trailing runs**, but only **6 of 14** on claude.ai.
- **Unjoinable** — the run has none *yet*. No shared address exists today, so the two sources cannot agree and must not both emit. Its address is `hook:` plus the delivery id, which is unique and needs no agreement. This class shrinks to nothing once a common id exists.

**Three address spaces, not two.** One frame can carry an unjoinable previous run *and* an honoured denial on its fresh round, and both would otherwise key on the same delivery id, merging two unrelated invocations into one record. A denial is therefore `deny:` plus the delivery id. Reader A still looks it up directly, because the activity names that delivery, and the prefix keeps it clear of the run that happened to arrive on the same frame.

### Ownership, not opportunistic joining

Because the join covers two runs in three, the capabilities are divided by ownership rather than left to meet in the middle.

- **The hook owns every invocation it sees.** Emit-previous: frame N carries the previous run's input and output both, so its record is complete on arrival. Its content is untruncated, which no reader's is.
- **Reader B emits standalone only for runs it can address**, where it can ask `seen` first. Today that means runs carrying a `tool_use` id; it is `joinable_address` that decides, not a tool-shaped test written into the reader. An unjoinable run it cannot address is left alone, because emitting it would be a second event for an invocation the hook already reported under a different key, and first-completed-wins counts that twice rather than reconciling it.
- **Reader B enriches only joinable records.** Attachment digests therefore reach **6 of 14** measured claude.ai rounds. The rest keep the extracted-text digest the frame supplied, which is exact for plain text and absent for processed documents.
- **Reader A is unaffected.** A denial has no run at all and is keyed on its own delivery id, which the activity names directly.

The cost is explicit, bounded and temporary: an **unaddressable run the hook never saw is never recorded**. That means a plain-text answer missed during a receiver outage, or skipped by a rollout percentage below 100. Tool-bearing runs, which are the ones that touch files and servers, are covered by both.

### The soft join, for file digests only

The `toolu_` anchor leaves attachment enrichment thin: measured, only **1 of 4** attachment-bearing rounds had a joinable run. A second, weaker join closes most of that gap without touching the addressing scheme.

**The conversation half is not soft at all.** Measured on every claude.ai conversation in the tenant: a frame's `session_id` **is** the chat's own identifier, the one its `href` ends with. Three of three matched exactly. So a reader holding a chat can scope candidate records to that one conversation for free, and every ambiguity that remains is ambiguity *within* a conversation.

**The timestamp half discriminates well, and says so when it does not.** Against each frame, the nearest user message was 0.3–6.9 s away and the next candidate at least 6.1 s further. But proximity alone is not enough: one measured chat had three frames inside 15 s of two messages, so "nearest" would have picked confidently and wrongly.

**The rule is therefore unanimity, not nearness.** A reader enriching a message's files counts the candidate records within `SLASHID_SOFT_JOIN_WINDOW_SECONDS` of it in the same conversation. **Exactly one candidate enriches; zero or several abstain.** Measured on the corpus, that lifts attachment coverage from 1 of 4 rounds to 3 of 4, and correctly declines the ambiguous one. The window is a knob because it matters: at ±15 s three rounds resolve uniquely, at ±60 s one of them gains a second candidate and abstains.

**A soft match may enrich and may never emit.** It adds digests to a record that already exists; it never creates one, never pushes one, and never decides an address. That is what bounds the damage of a wrong match to wrong hashes on one event, rather than a duplicated or misattributed invocation — which is the failure the whole addressing scheme exists to avoid. The `contributed` set records that compliance touched the record, so a reviewer can still tell a soft-joined event from a hard-joined one.

Everything above is scoped to `accessed_files`. Nothing else is enriched this way, because nothing else is worth a probabilistic match.

### The tail record

Emit-previous leaves the final round of a session with no successor to report it, and `accessed_files` are attributed to the round the model consumed, so a `Read` in that round would otherwise vanish. Every frame therefore writes a **tail record** holding its fresh round, input-only.

Its address is `tail:` plus a digest of the frame's **whole** transcript. That digest is a hook-local key and needs no cross-source agreement, which is exactly the property the measurement above says it can have: intra-source it was **exact — 239 distinct keys from 284 deliveries, zero false merges and zero false splits** against the `toolu_` ids as ground truth.

**The successor frame discards it.** Frame N+1 reconstructs frame N's transcript by dropping its own trailing assistant run and the round that follows it, computes the same `tail:` key, and discards that record unpushed — the fresh round is now covered properly by the run it fed. This needs no per-session pointer, which would have collided across the hundred-plus sub-conversations that share a `session_id`.

If no successor arrives, the deadline flush emits the tail as it stands. It is written in every configuration, because the reader never emits unjoinable runs and so can never duplicate it.

### Attribution runs one round behind

The record names the **previous** run, so its fields come from that run's round, not from the fresh one.

| Field | Comes from |
| --- | --- |
| `input` | the transcript up to and including the round the previous run consumed — **not** the fresh round |
| `output` | the previous assistant run |
| `used_tools`, `accessed_files` | the round that run consumed |
| the verdict | the **fresh** round, which is what was judged, and which belongs to the tail record |

Getting this wrong is easy and was caught in review: `after_last_assistant()` returns the *fresh* round and drives the verdict, but attribution needs the boundary one round earlier. Building the normalized invocation with the transcript truncated before the trailing run makes the shared helpers land on the right round by themselves, which is why the builder is handed a truncated transcript rather than the whole frame. An attachment in the fresh round belongs to the tail, never to the record being emitted.

### The pending record

The receiver never pushes from the request path. It writes a record and returns.

| From the frame | Outstanding |
| --- | --- |
| `identity_details`, `model`, `timestamp`, `conversation_id` | |
| `input`, and `output` from the trailing run | |
| `accessed_files` and `used_tools`, both from the consumed round | attachment byte digests, on an attachment-bearing round |
| `available_tools`, `available_tool_servers`, from observed names | |
| the verdict actually answered, and the composed one | |

**A frame-built record is complete on arrival except for attachment digests.** That is the whole of what a record can wait for, so the expectation set has exactly one member and the common case never waits. Measured: 475 Claude Code frames carried zero attachment blocks and 16 of 17 claude.ai frames carried at least one, so waiting is confined to the surface that needs it.

**The stored document is the event object itself**, as a serialized mapping rather than a validated model. `AIInvocationObservedV1` requires `parsed_as`, which depends on which sources end up contributing, and its base sets `extra="forbid"`, so the control envelope cannot ride inside it. Validation happens at push, on the one path that can log and retry. The 1 MiB document bound is enforced in the record module rather than the storage adapter, and dropping raw text sets an elision marker so the absence reads as elision rather than as an invocation that carried none.

The control envelope, which never reaches the wire:

| Envelope field | Why |
| --- | --- |
| `webhook_ids` | every delivery that revealed this invocation — 43 of 239 were revealed by more than one — so Reader A can match a denial against any of them |
| `deadline` | when the flush may push it |
| `verdict` / `composed_verdict` | what was answered and what the checks decided. Under shadow mode they differ, and without the second the rollout has nothing to show an operator. |
| `awaiting` | `file_digests`, or empty. Seeded **only when the round carries an attachment, compliance is enabled, and the run is joinable** — all three. The digests come from the compliance listing, so without a reader the expectation could never be cleared and the record would wait out the full deadline for nothing. |
| `contributed` | which sources actually supplied a field, which is not the same as which visited. `parsed_as` reads joined only when this holds more than one. |
| `attempts` / `tombstoned_at` | push failures, and retirement |

### Where the record lives

**Firestore, behind a port.** The choice follows `vertex/`, which already persists its polling watermark there, so the project has the database, the Terraform and the credentials path. It offers per-document atomic read-modify-write, a TTL policy and a query over an indexed field, which is what this needs and nothing more.

| Operation | Signature | Contract |
| --- | --- | --- |
| `upsert` | `(address, fields, expectations) -> Outcome` | create or merge. Creating sets the deadline and seeds expectations. Merging never moves the deadline. On a tombstoned address it is a no-op and says so. Returns whether this call left the record ready to push. |
| `complete` | `(address, fields, clears) -> Outcome` | merge and clear. No-op on a tombstoned address. Never creates: a record that does not exist was never opened by a frame, and inventing one here would resurrect a pushed invocation. |
| `claim` | `(address, lease) -> Record \| None` | take the exclusive right to push, for a bounded lease, and **return the record as it is now**. Every pusher calls it, the flusher included. |
| `due` | `(now, limit) -> list[Record]` | live records past their deadline whose claim is absent or expired, oldest first, bounded. |
| `retire` | `(address, outcome)` | `pushed` tombstones; `failed` releases the claim and sets a next-attempt time; `superseded` tombstones without pushing, which is how a successor frame discards a tail. |
| `seen` | `(address) -> Live \| Tombstoned \| Absent` | three states; absent is what lets a reader emit standalone. |

`claim` is the operation the earlier draft lacked, and three details of it are load-bearing.

**Readiness is a state, not a transition.** Asking "did this call empty the expectation set?" cannot arbitrate: a record born ready — which under emit-previous is most of them — has no transition at all and nobody would push it, while a flusher and a completer can both observe readiness and push twice. Under first-completed-wins that race decides whether the invocation lands with or without its digests. A compare-and-set on the claim settles it and the winner pushes.

**The claim is a lease, not a flag.** A push that fails after a claim must not orphan the record. `retire(failed)` releases it, and `due` also returns records whose lease has expired, so a crash between claiming and pushing is recovered on a later tick rather than leaving a live record nothing will ever collect.

**`claim` returns the record, and the pusher pushes what it returns.** Reading through `due` and pushing that snapshot would drop fields a completer merged in between — precisely the digests the wait exists for, and a push is a commitment that cannot be topped up.

`complete` reports readiness in its outcome for the same reason `upsert` does, so a completing writer knows to claim.

Everything backend-specific stays in the adapter: the 1 MiB cap, the TTL policy keyed off `tombstone_expires_at` and never applied to a live record, the named database, and the composite index behind `due`. Another cloud reimplements six methods and nothing above this line changes.

**A frame-built record is better hashed than a reader-built one.** Its `input` and tool-result digests come from an untruncated transcript, while a reader sees tool blocks capped at 10 KB. The one thing no frame supplies is an attachment's bytes, which is why that is the only thing a record waits for.

**A flush emits whatever the record holds, and expiry pushes rather than deletes.** Two classes have no compliance counterpart at all — zero-data-retention organizations, and the sub-conversations that share a `session_id` — so a deleted record there would be a lost event. A tail record usually flushes input-only, but one waiting on digests already holds its `output`, and that flush must carry it.

**A reader that finds nothing to add must still say so.** Digests are outstanding until a reader *visits* the message, not until it finds files, so a visit that turns up no listing clears the expectation and settles the record at once. Otherwise an attachment-bearing round whose listing never materializes waits the full deadline.

The tombstone's lifetime must exceed the latest a reader can still arrive: `SLASHID_JOIN_WAIT_SECONDS` plus `SLASHID_POLL_LAG_SECONDS` plus one tick, plus any reader backlog. `SLASHID_TOMBSTONE_TTL_SECONDS` defaults to `7200`. If the reader falls further behind than that, its tombstones expire before it re-walks and it re-emits — harmless in the graph, noisy in detections — so the backlog alarm is tied to this value rather than left unquantified.

**Push, then retire.** A crash between them re-pushes an event the terminal drops from the graph, though it still stores a raw event and re-runs detections. The reverse order loses the event outright. The ordering favours the duplicate because a missing audit record is the failure this product exists to prevent.

**Reader B's standalone emissions leave a tombstone.** A turn the hook never saw has no record, so nothing would stop the next tick emitting it again — which in a compliance-only deployment is every turn, once per tick it stays in the lagging window. A standalone emission is therefore an `upsert` under the same key followed by `retire`.

**What the terminal actually does, measured against `ng-evangelion`.** Dedup is keyed on `{org}:{connection}:{request_id}` and is **first-completed-wins**: once a copy completes, a later copy with the same key is discarded whole, with no merge and no overwrite.

- **A push is a commitment.** A record flushed without its digests cannot be topped up later. So `SLASHID_JOIN_WAIT_SECONDS` must sit beyond normal reader lag rather than being trimmed for latency.
- **A duplicate is cheap in the graph and not elsewhere.** The dedup guards the graph and BigQuery writes only. Each delivery still stores a raw event and re-runs the detections engine, which is keyed per delivery, so duplicates can mean duplicate alerts.
- **The window is 72 hours, sliding, and Redis-backed.** A replay after it is processed as new and double-counts usage and token totals.

A copy that failed before completing leaves no sentinel, so a genuine retry after a failure does the work. Dedup guards completed processing, not delivery.

### Denials

A denied call produces no response and therefore no successor frame, so its pending record can only be completed by a reader or flushed.

- **With compliance**, Reader A completes it from the activity and stamps `guardrail_intervened`. Since an activity exists only when the block actually happened, shadow-mode denials never produce phantom block records, and `SLASHID_SHADOW_MODE` leaves the correctness path.

  **A denial therefore waits, exactly as an attachment-bearing round does.** It is created with a `denial_activity` expectation whenever compliance is enabled, so it is not ready on arrival and its writer does not push it. Without that it would be pushed and tombstoned within seconds of the delivery, Reader A would find a tombstone rather than a live record, and the completion path — which is the only route by which the activity's authoritative confirmation and the **real client user agent** reach the event — would be dead code. If the reader never arrives, the deadline flush emits the record as it stands, which is the hook-only behaviour below.
**A denial the feed never confirms must not be emitted as one.** Measured in production: with our shadow mode off and claude.ai's shadow mode *on*, the receiver answers deny, claude.ai ignores it, the model reads the file, and no activity is ever recorded — because an activity exists only for a block that actually happened. The denial record then sits awaiting an activity that will never arrive, and the deadline sweep flushes it as a `guardrail_intervened` event asserting a block that did not occur. Both records for that one turn were observed: a phantom denial and the real content-addressed invocation beside it.

So when compliance is enabled, a denial record that reaches its deadline still awaiting the activity is **discarded, not flushed** — retired unpushed with a warning naming the delivery. The feed is authoritative for what was blocked and its silence past the poll lag is evidence. Nothing is lost by discarding: the invocation itself is reported truthfully by its own content-addressed record, and only the false claim goes. Asserting a block that never happened is worse than not mentioning one, which is the single place in this design where that is true — everywhere else a missing record is the worse failure.

This is a consequence of the frame carrying no enforcement-mode indicator, the fourth item on the README's list of what the provider should give us. With one, the receiver would know at verdict time whether its answer would be honoured and would never open the record.

- **Hook only**, there is no feed to confirm anything, so the flush stamps `guardrail_intervened` from **the verdict the receiver actually answered**, recorded on the record at verdict time. Re-reading `SLASHID_SHADOW_MODE` at flush time would be wrong: the flush can run an hour later, across a redeploy or a mixed-revision rollout, and would describe a configuration that never applied to this call. It stays an operator assertion the receiver cannot verify, and one the feed can later contradict.
- **A shadow-mode deny needs no special case at all**: the request runs, a successor frame arrives, and the record completes normally as the allowed invocation it turned out to be.

#### Denials are sticky, and the reason matters

Under enforcement, denied content stays in the transcript and keeps being denied. The verdict scans the round after the last assistant message; a denial prevents an assistant message; so the offending block remains in scope and **every later turn in that session is denied**, however innocuous. The session is unrecoverable and only a new one escapes. Three consequences:

- `deny_reason` must tell the person to **start a new conversation**. Anthropic's guidance to say what to change is unfollowable — the content is in a history nobody can edit. The forwarder appends one fixed sentence to whatever the denying check supplied.
- One incident produces **one denial event per subsequent turn**, each a real blocked delivery on a distinct `webhook-id`. A detection must group them itself: same `conversation_id` plus the same `accessed_files` digests is one incident.
- The resulting transcript shape — two consecutive user-role runs with no assistant turn between them — is how a post-denial call is recognised.

Scanning only the newest message would unwedge the session and is wrong: it would let the model read denied content as soon as one more message arrived.

### Verdict composition

One remote check. An earlier design ran two concurrently — a separate policy receiver that took the raw signed frame, beside preflight — but the graph policy check has moved into preflight itself (slashid/ng-evangelion#7796), so there is one call, one credential and one answer shape.

**Preflight.** `POST {SLASHID_ENDPOINT}/ip/nhi/events/ai-invocations/preflight` with the connection push token, which is the same credential the sink uses rather than a second one.

**The request body is an `AIInvocationObservedV1`** — the very object this service already builds — sent early and therefore incomplete, with `output`, `tokens` and everything the model has not produced yet simply absent. There is deliberately no preflight-specific request schema, so nothing has to be kept in step and the invocation is not built twice. The object to send is **the tail event**, the partial record for the fresh round, since that is the round being judged. The record for the previous run is a different invocation and must not be sent.

**The response is `{"deny_reasons": [...]}`**, always serialized, the empty array included. Allow is a length check and never a null check. Each reason is at most 500 characters, already deduplicated, and composed only from values we sent — the endpoint deliberately never names a matched graph resource, because that would turn "confirm a digest you already hold" into an enumeration primitive over the organization's graph.

**Preflight fails closed.** A server-side check that cannot complete denies, with a reason of its own, rather than allowing. So an empty list is a genuine all-clear, and `SLASHID_VERDICT_FAIL_MODE` applies only to *our* transport failures — a non-200, a timeout, an unparseable body — and never to a 200 with an empty list. The server keeps a kill switch that reverts to permissive without a deploy; nothing here depends on which way it is set.

**We pass our budget down.** `SlashID-Request-Timeout` carries the verdict budget, so the server bounds its work to what we will actually wait for rather than to a fixed per-check deadline.

**Every accessed file is sent, uncapped.** The server dropped its own cap once failing closed made flooding deny rather than slip through. A cap on our side would now be the bypass: a sensitive file past it would never be checked at all.

Only `accessed_files` is read for content, so the call is skipped when the fresh round has nothing hashable, and when `actor.id` is null.

**Composition.** Any deny denies — preflight, the hash knob, or the capture marker. The first denying check supplies `deny_reason`, to which the recovery sentence is appended. The base is truncated to `500 - len(sentence)` **before** appending, never the joined string, or a long upstream reason would silently delete the one sentence the person needs. A transport failure applies `SLASHID_VERDICT_FAIL_MODE` (default allow). A disabled check is skipped and does not count as a failure. `reference_id` is `hex(sha256(webhook-id))[:32]`, stable across retries of one delivery.

**`config-test` frames and frames of unknown top-level `type` bypass the check, answer allow, and write no record.** They carry no invocation; a pending record for one would have no successor frame and the flush would later push a console connection test as a real invocation against a real user. Denying an unknown type would also break the protocol's forward-compatibility rule.

**`SLASHID_SHADOW_MODE=true`**, the default, runs every check, logs the composed verdict, and answers allow. It is deliberately the same word claude.ai uses, because it is the same idea one layer down — and the two are independent, so a request is blocked only when *neither* is shadowed. The receiver cannot see the org's setting (no frame reveals it), which is why it keeps its own.

### Failure isolation — two rules

1. **An eventing failure must never become a verdict failure.** A non-200 is a *webhook failure*, which hands control to the organization's fail-open/fail-closed setting, and sustained failures trip Anthropic's circuit breaker and disable enforcement entirely. So: respond first, write the pending record in a tracked task afterwards, and never let its outcome reach the response.
2. **The verdict's own failure mode is a separate knob**, defaulting to allow. Two settings that are easy to confuse; the README must name both and say which covers what.

**A third check exists so the deny path can be exercised before preflight ships.** `SLASHID_MOCK_DENIED_HASHES` takes a comma-separated list of hex digests. When set, a local check denies any invocation whose `accessed_files` carry a matching `content_hashes` value, and it runs **in addition to** preflight rather than instead of it, composing under the same any-deny-denies rule. That matters for testing: with the endpoint unshipped and `SLASHID_PREFLIGHT_ENABLED` false, this is the only way to drive a real denial end to end — through composition, the deny reason, the recovery sentence, the `guardrail_intervened` stamp, the denial record's own address and Reader A's join — against a file whose digest the operator chose.

It is pure local computation over content the frame already carried, so it cannot fail and never interacts with `SLASHID_VERDICT_FAIL_MODE`. It judges the same tail event preflight judges, so a test denial and a real one are attributed identically. Unset, it costs a comparison against an empty tuple.

Like `SLASHID_CAPTURE_DENY_MARKER`, this is a test-tenant affordance and the README says so. Unlike the marker, it is content-addressed rather than a magic string, so it cannot be tripped by someone merely discussing it — which is the failure the marker has, and the reason this exists in its shape.

**Budget.** Anthropic's timeout is 1–10,000 ms, 5,000 default, covering the whole exchange, and it retries once after 100 ms only when the connection attempt fails. Preflight runs under `SLASHID_VERDICT_BUDGET_MS`, which is also what it is told as its own deadline.

### Reader A — denials, from the Activity Feed

Checkpointed on `created_at`, polling `inference_hooks_request_denied` and filtering its own `compliance_api_accessed` noise. **`order=asc` is mandatory.** The feed defaults to newest-first, so a reader that resumes from its saved watermark without it pages steadily further into the past and never sees a new denial. Completes the pending record the activity's `request_id` names, or emits standalone from the activity when there is none: identity from `actor.user_id`, `conversation_id`, `surface`, and the real client user agent. **`model` is absent from the activity**, so it is taken from the conversation's transcript when one is available and `"unknown"` otherwise.

### A run means two different things on the two sources

Measured on the live tenant, structure only. In a **frame**, a tool result arrives in the *following user message*, so one invocation spans an assistant message and the user message after it. In a **chat transcript**, the whole cycle sits inside one assistant message: a real one reads `tool_use, tool_result, text, tool_use, tool_result`.

That is why "run" has to be defined per source rather than assumed. On the hook side a run is a maximal sequence of consecutive assistant messages; on the chat side it is one message that may hold several tool cycles. Local session transcripts follow the frame's shape rather than the chat's, with tool results in user messages, so the reader cannot use one walk for both of its own feeds either.

The address survives this, which is the point of anchoring on a `tool_use.id`: whichever shape the source uses, the first tool use identifier in the run is the same token. A digest over the message sequence would not have survived it, which is a second independent reason the measurement killed that option.

### Reader B — responses, from the Compliance API

Polls local sessions and chats by `updated_at` with a lagging bound, filtered to `SLASHID_ORGANIZATION_UUID`. The two listings are separate feeds with separate watermarks, and **no two feeds share a query vocabulary**, so the client carries a small adapter per feed rather than one generic pager:

| Feed | Lower bound | Ordering | Page token |
| --- | --- | --- | --- |
| activities | `created_at.gte` | `order=asc`, default is `desc` | `last_id` |
| chats | `updated_at.gte`, rejected unless ordered | `order_by=updated_at` | `last_id` |
| local sessions | `updated_at.gte` | **no ordering parameter exists**; returns newest-first | `next_page` |

Local sessions being unorderable is the awkward one: the reader cannot stream forward from a watermark, so it drains the whole lagging window each tick and relies on the pending store's tombstones to suppress what it already emitted. That is affordable only because the window is bounded by `SLASHID_POLL_LAG_SECONDS` and the tick cadence.

**A truncated drain must not advance the watermark.** `SLASHID_MAX_SESSIONS_PER_TICK` cuts the listing at the newest sessions, so the untouched tail is the oldest. Advancing past it would lose those sessions permanently, hardest on the busiest tenants and immediately after any outage. So the watermark moves only on a drain that completed, and a tick that hits the cap logs how far behind it is. The failure mode that leaves is the opposite one: if arrivals exceed the cap every tick the reader never catches up and the window grows without bound. That is why the cap is a bound on *sessions* and the alert is on window age, not on tick duration.

**The first tick after a credential is added does not backfill.** `CheckpointStore.load()` answers an empty checkpoint on a cold start, and the vertex semantics for that are "fetch everything up to the batch bound" — here that would re-emit the whole retention window as standalone events. The initial watermark is `now - SLASHID_POLL_LAG_SECONDS` instead, and a backfill is an explicit opt-in rather than what happens by accident. Emits one invocation **per newly-produced assistant turn**, skipping `client_asserted` history, `synthetic_marker` messages and `content_unavailable` turns — the last being a turn whose content the API will not return, with a `reason` of `not_captured`, `client_aborted`, `cmek_key_revoked`, `retention_elapsed` or `oversize`. It never appeared in this tenant, which has no retention policy in force, but any customer with finite retention produces them, and emitting one would create a contentless invocation. The schema also tells callers to tolerate unrecognized `type` values, so an unknown provenance is skipped rather than rejected. Fills `output`, `used_tools` and `stop_reason` — **inferred from block shape**, exactly as the hook path infers it, because no surface supplies it.

**The hook goes first and the reader covers whatever it did not.** A turn the hook saw has a pending record the reader enriches. A turn the hook never saw — unsampled under a partial rollout, arriving while the receiver was down, or a session's final round — has none, and the reader emits it standalone with whatever the transcript gives: 10 KB-capped tool blocks, no untruncated digests, `model` only where the message carries one. Worse than a hook-emitted event, far better than nothing, and `parsed_as` says which it is.

### Attachment enrichment

Neither the file id nor the md5 is in the frame: an `attachment` block carries only `file_name`, `media_type`, `size_bytes` and `text`, so the hook side has nothing to hash but extracted text. Both come from the compliance `files[]`.

| `SLASHID_ATTACHMENT_HASHING` | Requests | Bytes through the collector | Digests |
| --- | --- | --- | --- |
| `md5` *(default)* | **none extra** | **none** | `md5`, when the listing carries one |
| `full` | one per attachment | the whole file | `md5`, `sha1`, `sha256` |

`md5` is free in both senses: the digest already rides along in a response Reader B fetches anyway, and no file bytes transit the collector. It is not a token tier — Salesforce-sourced graph resources carry md5 alone. `full` is the opt-in for sha1 and sha256, needed for OneDrive, SharePoint and Drive, and it means the collector downloads customer files.

**There is no HEAD to fall back on.** Measured: `HEAD` on the file content endpoint 404s on every attachment, so unlike the Gemini resolver there is no cheap metadata call. The message listing is the only source of `size_bytes` and `md5`, which is why both tiers below read from it rather than probing the object.

**`full` is capped, and the cap degrades gracefully.** `SLASHID_MAX_ATTACHMENT_FETCH_BYTES` bounds what is worth downloading; the listing's `size_bytes` is known before any fetch, so an oversized file is never started rather than aborted. This follows the sibling forwarders, where the Gemini `fileData` resolver takes byte length and md5 from a HEAD and only issues a GET when the object fits the configured cap, and **never hashes a partial fetch** — a ranged read yields a snippet, never a digest.

The degradation is better here than there. An oversized attachment falls back to the listing's `md5`, which is a whole-file digest we already hold, so the entry keeps a matchable hash and loses only sha1 and sha256. Vertex's equivalent fallback has no hash at all. The cap is therefore a bandwidth and tick-duration control rather than a coverage decision: a tick may touch 200 sessions, and a handful of large PDFs would otherwise dominate it.

**The reader replaces rather than pairs.** Pairing a frame's attachment block to a `files[]` entry is unreliable: `file_name` is null for images by documented behaviour and was null for this tenant's PDFs too, though the published example shows a named one, so the rule must not lean on it; the `<uploaded_files>` order does not match the block order, and `size_bytes` disagrees whenever the stored copy was processed. So for a message with `files[]`, entries whose `provenance` is `attachment` are rebuilt from the listing, which is better on every field. `tool_result` entries are never touched.

**An attachment is reported once, on the invocation that consumed it.** `files[]` hangs off the single user message that carried the upload, and the frame's round scoping excludes the block from later frames. Without that, a twenty-turn chat about one PDF would look like twenty accesses.

**The same file uploaded twice is two accesses.** Measured: two different `claude_file_…` ids, identical md5 and size, one per user message. The digest is an entry's identity; the id never is.

**Every digest here is of what Claude stored, not always what the user uploaded** — a processed image, or a document kept as extracted text. Such a hash will not match the original, and the README says so.

### `AIAccessedFile.provenance`

One new field, and what makes the replace rule implementable: without it the two kinds of entry are indistinguishable in a flat list.

| `provenance` | What it is | Digest is over |
| --- | --- | --- |
| `tool_result` | read through a tool — `Read` and its siblings | returned text, `cat -n` stripped, untruncated from a frame |
| `attachment` | uploaded by the user | extracted text from a frame; stored bytes from a reader |
| `generated` | *reserved, not emitted* — produced by the model through tool use | stored bytes, `md5` from `generated_files` |

`artifacts` stay out, and not for want of a digest. Measured: `GET /v1/compliance/apps/artifacts/{version_id}` returns `size_bytes` and an `md5` over the UTF-8 text, and a recomputation matched it exactly. They are excluded because an artifact is **not a file the conversation accessed**. It is the assistant's own output, it has no filename, and its digest would be over text we fetched once per edit, since every edit mints a new `version_id`. Recording it here would put model output in a field a reviewer reads as ingress. It belongs with `output`, which is where the open question puts it.

Per the standing convention that the server ignores unknown fields, this ships client-side first and the `ng-evangelion` sync batches with the other wire extensions.

### Field mapping

| Envelope field | Source |
| --- | --- |
| `request_id` | the completed run's first `tool_use.id`, else the `inv:` digest over that run; the frame's `webhook-id` on an answered denial and on a tail record |
| `identity_details` | `{kind: "anthropic", user_id: actor.id}`. A null `actor.id` drops the event — the server rejects an identity with no identifier. |
| `timestamp` | the attested `webhook-timestamp`, or the message's `created_at` on a reader-emitted event |
| `conversation_id` | `session_id` |
| `model` | `AIModel(id=model or "unknown", provider="anthropic", raw_model_id=model)`. `shared.model_catalog` is Bedrock-only and unused. |
| `input` / `output` | per the attribution rule above: `input` ends before the trailing run, `output` is that run |
| `available_tools` / `available_tool_servers` | synthesized from observed `tool_use` names via `build_tools_declared`; the frame carries no definitions |
| `used_tools` | the consumed round's `tool_result` blocks joined to their `tool_use` |
| `accessed_files` | per `provenance` above |
| `stop_reason` | `tool_use` when the run ends in a tool call, `end_turn` otherwise; `guardrail_intervened` on an enforced denial |
| `tokens` | zero — no surface carries usage |
| `user_agent` | `source.application`; the real client agent on a reader-emitted denial |
| `parsed_as` | `anthropic-inference-hook`, `anthropic-compliance`, or `anthropic-joined` |

## Module layout

One module, a package per source, a shared spine.

```
anthropic/
├── pyproject.toml                       # uv workspace member; depends on ../shared
├── Dockerfile                           # uv-built image, uvicorn entrypoint
├── README.md
├── src/slashid_anthropic_forwarder/
│   ├── config.py                        # one Config; capabilities derived from the credentials present
│   ├── main.py                          # FastAPI: POST /{path} is the hook, POST /tick drives readers and flushes
│   ├── pending.py                       # the record, readiness, the flush, tail supersession
│   ├── store.py                         # PendingStore protocol + FirestorePendingStore
│   ├── hook/
│   │   ├── signature.py                 # wraps the standardwebhooks reference library
│   │   ├── frame.py                     # PromptFrame + split_transcript
│   │   ├── capture.py                   # raw-frame capture, test tenants only
│   │   ├── checks.py policy.py preflight.py verdict.py
│   │   └── event_envelope.py            # frame → pending record
│   └── compliance/
│       ├── client.py                    # activity feed, session listing (clls_ decode), transcripts, chats, files
│       ├── checkpoint.py                # activity cursor, lagging updated_at bound
│       ├── attachments.py               # md5 from the listing, or download and digest
│       ├── denials.py                   # Reader A
│       └── responses.py                 # Reader B
├── tests/
└── deploy/terraform/
```

Two things belong in the spine rather than either package: `invocation_address`, a pure function over a completed assistant run that returns its first `tool_use.id` or an `inv:` digest of the run, so both sources compute byte-identical keys. It takes no ordinal: per-session ordinals collide across the sub-conversations that share a `session_id`. Its canonical encoding — field separators, UTF-8 normalization, message boundaries — is part of the function's contract, since two independently written call sites must agree byte-for-byte, and the event assembly they share. `CheckpointStore` should be promoted from `vertex/` into `shared/` rather than copied, and its `Checkpoint(timestamp, id)` type carries over unchanged. Per-cursor naming needs no work: `vertex/` already constructs one store per source with a distinct `document=`, one per region plus an audit-only document, so the constructor takes what this service needs. The real migration cost is that `Checkpoint` is defined in `vertex/`'s `event_source.py` beside BigQuery-specific types, so promoting it means extracting a type out of a Vertex-specific module and re-pointing vertex's own imports and tests.

Two cautions the move must carry with it. The watermark is persisted as a **timestamp, never as one of the feeds' opaque page tokens**, which the API documents as format-unstable; those tokens paginate within a tick and are then discarded. And a `(timestamp, id)` watermark is a resumable cursor only on the two ordered feeds — on local sessions it is a *window bound*, so `save()` after a partial drain is unsafe, as above.

### Reused from `shared/`

| Module | Used for |
| --- | --- |
| `normalize/anthropic/{schema,normalize}.py` | frame messages parse as `AnthropicRequestMessage`; two additions below |
| `normalize/turn.py` → `after_last_assistant()` | the fresh-round split driving the verdict and the attribution |
| `normalize/finalize.py`, `normalize/normalized/tool_results.py` | `accessed_files` from `Read`-style results, `cat -n` stripped, multi-algorithm, deduped |
| `normalize/normalized/tools.py` → `build_tools_declared()` | synthesized tool and server lists |
| `events.py` | the wire models and `build_event_from_normalized` |
| `sink.py` | the push, batching, retry classification |
| `config_base.py` | `SLASHID_ENDPOINT` / `_PUSH_TOKEN` / `_INCLUDE_RAW_CONTENT` / `_MAX_CONTENT_SIZE` |

Five shared additions: `AnthropicIdentityDetails` in the `IdentityDetails` union; `AnthropicToolUseBlock.name` gaining `AliasChoices("name", "tool_name")`; an `AnthropicAttachmentBlock` translating to `kind="document"`; `AIAccessedFile.provenance`; and `conversation_id` on `EventEnvelope`, which today has no such field, so `build_event_from_normalized` cannot populate the one the field mapping requires on every event. Adding it there rather than patching the built event keeps the other forwarders' path identical. `parse_media_type` also needs fixing — it claims to reject unregistered types but never does, so an unregistered `media_type` currently raises instead of falling back to `None`.

## Configuration

`Config(BaseConfig)` adds:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SLASHID_HOOK_SIGNING_SECRET` | unset | `whsec_…`; comma-separated accepts any number, tried in order. **Setting it enables the hook.** |
| `SLASHID_PREFLIGHT_ENABLED` | `false` | call `{ENDPOINT}/ip/nhi/events/ai-invocations/preflight`; keep off until that endpoint is deployed |
| `SLASHID_VERDICT_FAIL_MODE` | `allow` | `allow` or `deny` when a check fails or answers unverified |
| `SLASHID_VERDICT_BUDGET_MS` | `3500` | both checks, concurrently, under Anthropic's timeout |
| `SLASHID_SHADOW_MODE` | `true` | our own shadow mode, named after claude.ai's `shadow_mode` field and **independent of it**: when either is on, nothing is blocked. On by default, so a fresh deployment observes before it enforces. |
| `SLASHID_MAX_BODY_BYTES` | `33554432` | Cloud Run's HTTP/1 limit |
| `SLASHID_JOIN_WAIT_SECONDS` | `3600` | deadline before an unsettled record is pushed as it stands |
| `SLASHID_TOMBSTONE_TTL_SECONDS` | `7200` | how long a pushed record's tombstone suppresses a late reader's duplicate; must exceed `JOIN_WAIT` + `POLL_LAG` + one tick |
| `SLASHID_GCP_PROJECT_ID` | required with the store | the project holding Firestore; `vertex/` has the same field and the anthropic `Config` does not yet |
| `SLASHID_FIRESTORE_DATABASE` | `slashid-anthropic` | the named database. `vertex/` names its own `slashid-vertex` rather than using `(default)`, and this follows that. |
| `SLASHID_PENDING_COLLECTION` | `anthropic_pending` | collection holding pending records and their tombstones |
| `SLASHID_MAX_FLUSHES_PER_TICK` | `500` | bounds `due` so one tick cannot stall behind a backlog |
| `SLASHID_PUSH_BUDGET_MS` | `2000` | bounds the push task; the sink's own retries are inert under it |
| `SLASHID_COMPLIANCE_KEY` | unset | `sk-ant-api01-…`. **Setting it enables the readers.** |
| `SLASHID_ORGANIZATION_UUID` | unset | required with the key; it can read every linked organization, so readers filter to this one |
| `SLASHID_POLL_LAG_SECONDS` | `120` | how far behind now the `updated_at.gte` bound sits |
| `SLASHID_MAX_SESSIONS_PER_TICK` | `200` | bounds a tick against the 600 rpm shared with the sync adapter |
| `SLASHID_ATTACHMENT_HASHING` | `md5` | `md5` or `full` |
| `SLASHID_MAX_ATTACHMENT_FETCH_BYTES` | `10485760` | under `full`, the largest attachment worth downloading. Decided from the listing's `size_bytes` before any fetch; an oversized file falls back to the listing's `md5` rather than to no digest. |
| `SLASHID_CAPTURE_BUCKET` | unset | raw-frame capture for protocol study; test tenants only |
| `SLASHID_CAPTURE_DENY_MARKER` | unset | a token that forces a deny, for end-to-end enforcement tests on a test tenant; never logged or echoed |
| `SLASHID_MOCK_DENIED_HASHES` | unset | comma-separated hex digests; denies any invocation whose `accessed_files` match one. Runs **alongside** preflight, so the deny path is testable before that endpoint ships. Test tenants only. |

**At least one credential must be present**, or startup fails. The signing secret is required only when the hook is in use, so compliance-only needs none.

**Two conflicts with the code already on the branch**, both to settle in the implementation plan rather than silently:

- `config.py` validates that a signing secret is present. That makes compliance-only impossible to start today. The validator has to become conditional on the hook capability being in use.
- `config.py` defaults `preflight_enabled` to `true`, against the `false` above. The table is the intent, since the preflight endpoint has not shipped; the code default flips.

## Tests

Fixture-driven, matching the house pattern, with `yaml_pytest` case tables over captured frames and recorded API responses.

- `test_signature.py` — valid, tampered, stale, future-dated, unsigned, malformed secret, several candidate signatures, re-cased headers, N secrets tried in order, and a secret whose base64 contains `+` and `/`.
- `test_frame.py` — unknown block type, unknown `source.application`, unknown `actor.type`, unknown top-level `type`, null `session_id`/`model`; the transcript split, including a consumed round spanning two user messages and a merged assistant run.
- `test_preflight.py` — preflight sends the **tail** event and never the previous run's; an empty `deny_reasons` is an allow and not an unverified; a non-empty one denies and its reasons compose the message; non-200 and timeout raise and apply the fail mode; every accessed file is sent, uncapped, and the verdict budget goes as `SlashID-Request-Timeout`.
- `test_verdict.py` — both allow; each deny wins with its reason; transport failure and unverified honour the fail mode; budget exceeded; `config-test` and unknown type bypass; observe-only allows while still evaluating; the recovery sentence is appended; `reference_id` charset.
- `test_pending.py` — a record is written rather than pushed in every configuration, the hook alone included; the next frame settles a no-attachment round immediately; an attachment-bearing round waits; a reader's visit that finds no listing settles it too; **a record past the deadline is pushed, not deleted**; a flush carries whatever the record holds, including `output` a successor frame already supplied; a pushed record leaves a tombstone; a tail is written in every configuration and its successor discards it unpushed; `claim` lets exactly one of a completing writer and the deadline sweep push.
- `test_store.py` — against a fake and, when credentials allow, the Firestore emulator: `upsert` twice does not move the deadline; two concurrent `complete` calls both land, neither lost; `due` excludes tombstoned records and honours its bound; `seen` distinguishes live, tombstoned and absent; an oversized `input` is dropped rather than failing the write.
- `test_event_envelope.py` — the key is identical when computed from the frame that creates a record and from the frame that completes it, and when computed by a reader from the stored transcript; it survives a tool block truncated at 10 KB; it never changes once set; consumption attribution; `cat -n` stripped; null `actor.id` drops.
- `test_client.py` — `clls_` decode yields the frame's `session_id`; synthetic and `client_asserted` messages are skipped; truncation surfaced; own `compliance_api_accessed` filtered; a session outside the bound organization skipped.
- `test_denials.py` / `test_responses.py` — an activity completes or emits standalone; `model` falls back; one event per blocked attempt; a newly-produced turn's address is **byte-identical to the hook path's for the same run**, computed from the frame and from the stored transcript over the captured corpus, and a standalone emission leaves a tombstone that the next tick honours.
- `test_attachments.py` — `md5` takes the listing digest and makes no request; `full` downloads and its md5 equals the listing's; a file over `MAX_ATTACHMENT_FETCH_BYTES` is never requested and keeps the listing's md5 alone; a listing with no md5 yields an entry with no digest; `attachment` entries are replaced and `tool_result` entries preserved.
- `test_main.py` — a sink failure still returns 200 with the correct verdict (**rule 1**); a slow sink does not delay the response; oversized body; unsigned gets 401.

## Deployment

Cloud Run, one Terraform module attached to the release, mirroring `vertex/deploy/terraform` in shape.

- The release workflow builds the image with `uv`, pushes it to GHCR, and the module pulls it through an Artifact Registry remote repository proxying `ghcr.io` (credentials required while the repo is private).
- **One service, two routes.** `POST /{path}` is the hook; `POST /tick` drives the readers and the deadline flush, fired by Cloud Scheduler with an OIDC token. Cloud Run's per-instance concurrency does **not** serialize ticks — a second concurrent request gets a second instance — and Scheduler retries on timeout without suppressing overlap, so overlapping ticks are the steady state under load rather than an edge case. A tick therefore takes a **Firestore lease** before doing any work and exits immediately if another holds it.
- **The tick cadence is a declared input, not only a cron string**, because `SLASHID_TOMBSTONE_TTL_SECONDS` must exceed `JOIN_WAIT` + `POLL_LAG` + one tick and the service cannot check an inequality against a number it never sees. Note the default `JOIN_WAIT` of 3600 plus `POLL_LAG` of 120 leaves only 3480 s of tick interval under a 7200 s tombstone, so the hook-only "slow tick" cannot be hourly. Startup asserts the inequality and refuses to run if it fails.
- **Hook only**: `min_instance_count = 1`, since a cold start inside the verdict budget risks a webhook failure and enough of those trip the circuit breaker. Needs the pending store and a slow tick to fire flushes; no compliance polling.
- **Compliance only**: no public endpoint, no certificate, no minimum instance — a scheduler, a checkpoint store and two secrets.
- Secret Manager holds the push token, the signing secret and the compliance key. Firestore holds the pending store and checkpoints, in a named database as `vertex/` does, behind the port above. Its TTL policy keys off **`tombstone_expires_at`**, a separate field, and never applies to a live record. Firestore deletes when the nominated instant is *past*, so pointing the policy at `tombstoned_at` would collect every tombstone the moment it was written and give it no lifetime at all. `retire` writes `tombstone_expires_at = now + SLASHID_TOMBSTONE_TTL_SECONDS` on a successful push and leaves it unset on a failure, so a record still being retried is never collected. `tombstoned_at` keeps its own meaning, which every readiness check tests for presence. Keying it off creation time instead — the obvious implementation given the 7200 default — would delete a record that had been failing to push for two hours before it was ever emitted, which is precisely the loss the store exists to prevent. Flushing must emit, so it is the tick's job, not the TTL's. The module also provisions the composite index `due` needs.
- Cloud Run caps HTTP/1 bodies at 32 MiB, under the protocol's 64 MiB ceiling.

## Rollout

Anthropic provides staged rollout server-side, so use it: shadow mode, then a rollout percentage, then role exclusions, then enforcement with the customer's choice of fail-open or fail-closed. Ship `SLASHID_SHADOW_MODE=true` so the receiver is observe-only until a customer opts in.

**With a compliance credential present, a low rollout percentage stops being a coverage decision and becomes purely an enforcement one**: turns the hook never saw are emitted by the reader from the transcript.

## Known limitations

- **No tool or MCP-server inventory.** `available_tools` lists only tools actually *used*, by name, with no description or schema. `bedrock/` and `vertex/` read real declarations from the request body, so absence here means unobservable, not unused. On the **frame** an MCP server is visible only when a client names its tools `mcp__server__tool`. On **claude.ai chat messages** the reader does better: tool blocks carry `integration_name` and `mcp_server_url` as explicit fields, and `integration_name` was populated live (`File Creation`). So server attribution is partly recoverable on one surface, tool *definitions* on neither.
- **No `stop_reason` and no tokens** from any surface; both are inferred or zero.
- **Server-tool results are placeholders**, so content Anthropic's own tools fetch is outside every check, and nothing marks a call server-executed.
- **claude.ai's extended research emits no frames**, so an agentic task's fetches are entirely uninspected.
- **Hash matching from a frame covers plain text only.** A reader closes this for files Claude stores intact, but stored bytes are not always the upload.
- **Sticky denials** wedge a session permanently.
- **`conversation_id` merges sub-conversations**, since Haiku status frames and `web_search` sub-requests share the main session's id.
- **Reader-emitted events are 10 KB-capped** per tool block.
- **Two sources can only be joined on a `tool_use` id.** Measured on one session present in both: transcript-prefix digests produced 200 frame keys and 302 reader keys with **zero in common**, because the stored transcript is a different projection — a prepended synthetic marker, turns from before capture, sub-agent turns. So a run with no tool call is owned by the hook alone, and one the hook never saw is never recorded. Tool-bearing runs, the ones that touch files and servers, are covered twice.
- **Attachment digests reach fewer rounds than attachments.** Enrichment needs a joinable run, and only **6 of 14** measured claude.ai rounds had one. The rest keep the frame's extracted-text digest, exact for plain text and absent for processed documents.
- **The local-sessions listing cannot be ordered**, so Reader B re-walks its whole lagging window every tick instead of resuming from a cursor. Dedup absorbs the repeats; a long outage still means a long re-walk.
- **Cloud Run's 32 MiB body cap** is below the protocol's ceiling; observed frames peak at 1.86 MB.

## Open questions

1. **Does `actor.id` resolve against what the `anthropic` adapter stores for org members**, so `ResolveAIInvocationIdentity` succeeds on the first invocation rather than never?
2. **Should a seat-authenticated `actor.type: user` on an interactive surface set `HumanDriven`?** Decided yes; it needs a new `Reason` value server-side, batched with the schema sync.
3. **Closed.** `reference_id` was a candidate join key. It is not one: measured against `ng-evangelion`, the gate's `reference_id` is generated per decision, returned in the verdict, handed to an audit callback and written to a log line. Nothing persists it, nothing indexes it, and nothing correlates it to an invocation row. Gate decisions and observed invocations are not joined anywhere in that repo. Keeping the Go receiver's recipe therefore costs nothing and changing it gains nothing, so the recipe stays and the join is the denial activity's own `request_id`.
4. **When should `generated_files` be enriched?** Deferred, deliberately. They are field-identical to `files[]` apart from `created_at`, so the enrichment itself is nearly free, but the frame never reveals that a tool wrote a file. Covering them means waiting on every round from a file-capable surface instead of every attachment-bearing round, and that cost is not worth paying before the feature has a user. Revisit when a customer runs claude.ai file creation in anger. `provenance` reserves the `generated` value for that day and ships without it.
5. **Should artifact content be fetched into `output`?** It is the one way to close the artifact gap in Known limitations, at one request per artifact version. It wants its own knob, not a fold into file enrichment.

## References

- [Inference hooks overview](https://platform.claude.com/docs/en/manage-claude/inference-hooks), [endpoint protocol](https://platform.claude.com/docs/en/manage-claude/inference-hooks-endpoint), [configuration](https://platform.claude.com/docs/en/manage-claude/inference-hooks-configuration)
- [Compliance API setup](https://platform.claude.com/docs/en/manage-claude/compliance-api-access), [Activity Feed](https://platform.claude.com/docs/en/manage-claude/compliance-activity-feed), [session transcripts](https://platform.claude.com/docs/en/manage-claude/compliance-sessions), [chats, files and projects](https://platform.claude.com/docs/en/manage-claude/compliance-content-data)
- [Standard Webhooks](https://www.standardwebhooks.com/)
- `ng-evangelion`: [#7733](https://github.com/slashid/ng-evangelion/pull/7733) and `docs/superpowers/specs/2026-09-18-ai-preflight-endpoint-design.md`; `2026-09-13-ai-access-hooks-policy-design.md` and `backend/modules/detections/components/aiauthorization/README.md`
- This repo: `docs/superpowers/specs/2026-08-27-vertex-ai-forwarder-design.md`
