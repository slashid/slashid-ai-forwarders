# Anthropic AI-invocation collection — inference hooks and compliance pull

**Date:** 2026-09-21
**Status:** Design, ready for planning. Every wire claim below was measured against a live Claude Enterprise tenant on 2026-09-20/21; nothing here is inferred from documentation alone.
**Supersedes:** `2026-09-18-anthropic-inference-hooks-design.md`, which described the hook in isolation.
**Target repo:** `slashid-ai-forwarders`, new subdirectory `anthropic/`, beside `bedrock/`, `vertex/` and `shared/`.
**Companion changes:** `POST /ip/nhi/ai/preflight` in `ng-evangelion` ([#7733](https://github.com/slashid/ng-evangelion/pull/7733)); a batched schema sync for `AIAccessedFile.provenance` and `AnthropicIdentityDetails`.

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

- **Size, and it is bimodal by surface.** Claude Code: median 664 KB, p90 1.64 MB, **max 1.86 MB** (a 561-message transcript). claude.ai: median 3.4 KB, max 14.8 KB. Receipt-to-response was 39–42 ms for the largest, so the verdict budget is entirely outbound calls. Chaining to the policy receiver re-uploads the whole frame, and that receiver's cap is 10 MiB.
- **`webhook-id` is per delivery, not per invocation.** All 492 deliveries carried a distinct `webhook-id`, but they revealed only **239 invocations**: 284 frames carried a previous assistant run, and **43 invocations were revealed by more than one delivery**, 45 redundant deliveries in all. Keying events on the delivery would double-count 18% of invocations. The cause is structural — a transcript's trailing assistant run stays trailing until the model produces a new one.
- **The content anchor is available for most invocations but not all.** Of the 239, **175 anchor on a `toolu_` id** and 64 fall back to the content hash, so roughly one invocation in four depends on the fallback recipe agreeing byte-for-byte across sources.
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

### The pending invocation

The receiver never pushes directly. In every configuration it writes a **pending invocation** — an `AIInvocationObservedV1` built as far as the frame allows — and something later completes and pushes it. One code path throughout.

| From the frame | Outstanding |
| --- | --- |
| `identity_details`, `model`, `timestamp`, `conversation_id` | `output`, `stop_reason`, `used_tools` |
| `input` — untruncated hashes; text only under `include_raw_content` | |
| `accessed_files` for tool results, untruncated | `accessed_files` attachment byte digests, on an attachment-bearing round |
| `available_tools`, `available_tool_servers`, synthesized from observed names | |
| our own verdict, so a flush can stamp it | |

**The stored document is the event object itself.** There is no parallel struct to keep in step with the wire type: the record is a partial `AIInvocationObservedV1`, serialized the way it will be pushed, with the fields nothing has supplied yet simply unset. Completing it is assigning those fields. Flushing it is pushing what is there. The table above is therefore not a mapping between two shapes, it is a list of which fields of one object are set on arrival.

Beside the event sits a small control envelope that never reaches the wire:

| Envelope field | Why it exists |
| --- | --- |
| `webhook_id` | the delivery that created the record, so Reader A can join on the denial activity's `request_id` |
| `deadline` | when the flush may push it |
| `verdict` | the answer the receiver actually gave, so a flush stamps what happened rather than current configuration |
| `awaiting` | which enabled capabilities are still expected to contribute; a reader's visit clears its own entry whether or not it found anything |
| `tombstoned_at` | set after a successful push, retiring the record without deleting it |

The event half holds derived facts, a few kilobytes, never the 664 KB median Claude Code transcript. Its `accessed_files` are grouped by `provenance` so a reader can replace the `attachment` group without touching `tool_result`.

`parsed_as` lives on the event, not the envelope, and it is not decided until the push: it reads `anthropic-joined` as soon as a second source has contributed to the record, whatever order they arrived in.

**One size bound to assert rather than assume.** Firestore caps a document at 1 MiB. The event stays far below that unless `SLASHID_INCLUDE_RAW_CONTENT` is on, when `input` text runs to `SLASHID_MAX_CONTENT_SIZE`. The 100,000-character default is comfortably inside the cap, but the two settings are coupled, so the store checks the serialized size and drops `input` text rather than failing the write.

**The record is addressed by content, not by delivery.** Three things complete a record, and only one of them holds a `webhook-id`: Reader A, which reads it off the denial activity. The next frame carries its own new delivery id, not the previous round's, and Reader B never sees a `webhook-id` at all. So the document id is the **content address** — the same value the event's `request_id` will carry — and `webhook-id` is an attribute. That also collapses the 18% of invocations revealed by two deliveries into one record, where keying on the delivery would leave two records to flush, race and duplicate.

A tool-free run has no content address both sources agree on, so it is stored under its delivery id, is never joined, and flushes unenriched. That is the same limitation the addressing section states, surfaced here as a storage rule.

#### Where the record lives

**Firestore, behind a port.** The choice follows `vertex/`, which already persists its polling watermark there, so the project has the database, the Terraform to provision it and the credentials path. It gives per-document atomic read-modify-write, a TTL policy, and a query over an indexed field, which is exactly the three things this design needs and nothing more.

The design does not depend on that, and the store is a `Protocol` with a Firestore implementation beside it, the same shape `CheckpointStore` already uses. Five operations:

| Operation | Contract |
| --- | --- |
| `upsert` | create the record under its address, or merge into the existing one. Creating sets the deadline; merging never moves it. |
| `complete` | atomically merge supplied fields and clear one `awaiting` entry. Must be read-modify-write under contention, not last-write-wins. |
| `due` | records whose deadline has passed and which are not tombstoned, bounded per call. |
| `retire` | tombstone after a successful push. |
| `seen` | whether an address is live or tombstoned, so a reader can tell a turn the receiver never saw from one it already emitted. |

`complete` is the one with teeth. The hook path writes from a background task on any autoscaled instance, and a reader tick writes from another, so two completions of one record genuinely race. Firestore transactions cover it. Any replacement backend must offer an equivalent, or it is not a candidate.

Everything backend-specific stays in the adapter and out of the design: the 1 MiB document cap, the TTL policy as a backstop, the named database, and the composite index that makes `due` cheap. A future deployment on another cloud reimplements those five methods and changes nothing above this line.

### One predicate decides when to push

**A record is pushed once nothing an *enabled* capability could still supply is outstanding, or the deadline expires.**

- `output`, `stop_reason` and `used_tools` are supplied by the next frame, always, or by a reader when compliance is enabled.
- Attachment byte digests are supplied by a reader only, and only when the round contains an attachment.

With the hook alone nothing but the next frame can add anything, so a record settles the moment that frame arrives — the latency and content of plain emit-previous. With compliance also enabled, only an attachment-bearing round waits. Measured across the 492-frame corpus: **475 Claude Code frames contained zero attachment blocks; 16 of 17 claude.ai frames carried at least one**, several carrying three,, because Claude Code moves file contents through `Read`, which the frame already hashes untruncated. So most rounds never wait even with both capabilities on.

A frame-completed record is **better hashed** than a reader-completed one: its `input` and tool-result digests come from an untruncated transcript. The one thing no frame supplies is an attachment's bytes.

**Expiry pushes rather than deletes**, and that is what keeps coverage at or above hook-only. Two classes have no compliance counterpart at all — zero-data-retention organizations, and the sub-conversations that share a `session_id` — and a deleted record there would be a lost event. A flush emits **whatever the record holds**. Usually that is input-only — identity, model, untruncated `input` and `accessed_files`, no `output` — because nothing ever completed it. But a record can already hold `output`, `stop_reason` and `used_tools` from a successor frame and still be waiting on attachment digests a reader never delivered. That flush must carry the output it has. Discarding it would make a waiting record strictly worse than a hook-only one, which inverts the reason the wait exists.

**A reader that finds nothing to add must still say so.** The digests are outstanding until a reader *visits* the message, not until it finds files, so a visit that turns up no listing clears the expectation and settles the record at once. Without that stamp an attachment-bearing round whose files never materialize sits for the full `SLASHID_JOIN_WAIT_SECONDS`, and the join degrades into an hour-long delay.

**That flush is why the store exists, in every configuration including hooks alone.** `accessed_files` are attributed to the round the model consumed, so a `Read` in a session's final round belongs to the final invocation — the one no successor frame will ever report. Without the deadline flush, a user who reads a sensitive file and then closes the session produces **no audit record of that read at all**, which is the exact event this product exists to capture. The receiver is therefore not stateless, and the store is not optional: that was a design goal the measurements retired.

**Exactly one push per record.** A pushed record is retired with a tombstone rather than deleted, so a reader arriving after a deadline flush does not mistake the turn for one the receiver never saw and emit a duplicate. The tombstone keeps the record's content address, since that is the only key a reader can look up.

Its lifetime must exceed the latest a reader can still arrive: `SLASHID_JOIN_WAIT_SECONDS` plus `SLASHID_POLL_LAG_SECONDS` plus one tick. `SLASHID_TOMBSTONE_TTL_SECONDS` defaults to `7200`, comfortably above that sum at the default settings, and the Firestore TTL policy is a backstop that must never be set below it.

**Push first, then tombstone.** A crash between the two re-pushes an event the terminal dedups on `request_id`; the reverse order loses it silently. Every rule in this section bends the same way, because a duplicate costs nothing and a missing audit record is the failure this product exists to prevent.

### Denials

A denied call produces no response and therefore no successor frame, so its pending record can only be completed by a reader or flushed.

- **With compliance**, Reader A completes it from the activity and stamps `guardrail_intervened`. Since an activity exists only when the block actually happened, shadow-mode denials never produce phantom block records, and `SLASHID_SHADOW_MODE` leaves the correctness path.
- **Hook only**, the flush stamps `guardrail_intervened` from **the verdict the receiver actually answered**, recorded on the record at verdict time. Re-reading `SLASHID_SHADOW_MODE` at flush time would be wrong: the flush can run an hour later, across a redeploy or a mixed-revision rollout, and would describe a configuration that never applied to this call. It stays an operator assertion the receiver cannot verify, and one the feed can later contradict.
- **A shadow-mode deny needs no special case at all**: the request runs, a successor frame arrives, and the record completes normally as the allowed invocation it turned out to be.

#### Denials are sticky, and the reason matters

Under enforcement, denied content stays in the transcript and keeps being denied. The verdict scans the round after the last assistant message; a denial prevents an assistant message; so the offending block remains in scope and **every later turn in that session is denied**, however innocuous. The session is unrecoverable and only a new one escapes. Three consequences:

- `deny_reason` must tell the person to **start a new conversation**. Anthropic's guidance to say what to change is unfollowable — the content is in a history nobody can edit. The forwarder appends one fixed sentence to whatever the denying check supplied.
- One incident produces **one denial event per subsequent turn**, each a real blocked delivery on a distinct `webhook-id`. A detection must group them itself: same `conversation_id` plus the same `accessed_files` digests is one incident.
- The resulting transcript shape — two consecutive user-role runs with no assistant turn between them — is how a post-denial call is recognised.

Scanning only the newest message would unwedge the session and is wrong: it would let the model read denied content as soon as one more message arrived.

### `request_id` is content-addressed

The event's `request_id` identifies an **invocation**, not a delivery.

- Anchor on the assistant run's first `tool_use.id` (`toolu_…`) when present — model-minted, globally unique, and **identical on both sources**.
- Otherwise `"hook:" + hex(sha256(session_id or "", assistant-run ordinal, text digest))[:32]`. The prefix keeps it disjoint from `toolu_` ids and from every frame id Anthropic mints, which include `req_…`, `msg_…` and `chatcompl_…`.

Every re-reveal of a turn converges on one key, and the processor's terminal dedup drops the repeat. This is what makes two sources safe: both compute the same key for the same invocation. **For a tool-free run the fallback is source-dependent** — ordinals, truncation and the synthetic marker all differ — so such a run flushes unenriched rather than risking a wrong join.

A **denial** record instead uses the frame's own `request_id` (the `webhook-id`), which is correct for the same reason: a denial is a delivery-level event, and two blocked attempts are two incidents.

### Verdict composition

Two checks, concurrent, ANDed, each optional.

**Policy.** `POST {SLASHID_POLICY_URL}` with the **raw request bytes** and the three `webhook-*` headers verbatim, uncompressed. The `ng-evangelion` receiver re-verifies the signature under its own copy of the secret, checks the tenant binding, resolves the actor and evaluates graph policy. It answers HTTP 200 with the Anthropic verdict shape and turns its own errors into an explicit deny, so a 200 deny is authoritative and never softened by our fail mode.

**Preflight.** `POST {SLASHID_ENDPOINT}/ip/nhi/ai/preflight` with the connection push token: identity, model when present, and `accessed_files` with multi-algorithm hashes. Read `overall` only — `allowed: false, verified: true` denies with its `message`; `verified: false` applies the fail mode. Not called when the fresh round has nothing hashable, nor when `actor.id` is null. More than 100 files or a body over 1 MiB is treated as unverified.

**Composition.** Any deny denies. The first denying check supplies `deny_reason`, to which the recovery sentence is appended, truncated to 500 characters. A transport failure or an unverified answer applies `SLASHID_VERDICT_FAIL_MODE` (default allow). A disabled check is skipped and does not count as unverified. `reference_id` is `hex(sha256(webhook-id))[:32]`, matching the Go receiver's recipe.

**`config-test` frames and frames of unknown top-level `type` bypass both checks and answer allow.** The policy receiver denies unresolvable actors and non-`prompt` frames, so forwarding either would return an authoritative deny — the opposite of the protocol's forward-compatibility rule.

**`SLASHID_SHADOW_MODE=true`**, the default, runs every check, logs the composed verdict, and answers allow. It is deliberately the same word claude.ai uses, because it is the same idea one layer down — and the two are independent, so a request is blocked only when *neither* is shadowed. The receiver cannot see the org's setting (no frame reveals it), which is why it keeps its own.

### Failure isolation — two rules

1. **An eventing failure must never become a verdict failure.** A non-200 is a *webhook failure*, which hands control to the organization's fail-open/fail-closed setting, and sustained failures trip Anthropic's circuit breaker and disable enforcement entirely. So: respond first, write the pending record in a tracked task afterwards, and never let its outcome reach the response.
2. **The verdict's own failure mode is a separate knob**, defaulting to allow. Two settings that are easy to confuse; the README must name both and say which covers what.

**Budget.** Anthropic's timeout is 1–10,000 ms, 5,000 default, covering the whole exchange, and it retries once after 100 ms only when the connection attempt fails. The policy receiver caps itself at 2.5 s and preflight's graph deadline is 750 ms; both run concurrently under `SLASHID_VERDICT_BUDGET_MS`.

### Reader A — denials, from the Activity Feed

Checkpointed on `created_at`, polling `inference_hooks_request_denied` and filtering its own `compliance_api_accessed` noise. **`order=asc` is mandatory.** The feed defaults to newest-first, so a reader that resumes from its saved watermark without it pages steadily further into the past and never sees a new denial. Completes the pending record the activity's `request_id` names, or emits standalone from the activity when there is none: identity from `actor.user_id`, `conversation_id`, `surface`, and the real client user agent. **`model` is absent from the activity**, so it is taken from the conversation's transcript when one is available and `"unknown"` otherwise.

### Reader B — responses, from the Compliance API

Polls local sessions and chats by `updated_at` with a lagging bound, filtered to `SLASHID_ORGANIZATION_UUID`. The two listings are separate feeds with separate watermarks, and **no two feeds share a query vocabulary**, so the client carries a small adapter per feed rather than one generic pager:

| Feed | Lower bound | Ordering | Page token |
| --- | --- | --- | --- |
| activities | `created_at.gte` | `order=asc`, default is `desc` | `last_id` |
| chats | `updated_at.gte`, rejected unless ordered | `order_by=updated_at` | `last_id` |
| local sessions | `updated_at.gte` | **no ordering parameter exists**; returns newest-first | `next_page` |

Local sessions being unorderable is the awkward one: the reader cannot stream forward from a watermark, so it drains the whole lagging window each tick and relies on the pending store's tombstones to suppress what it already emitted. That is affordable only because the window is bounded by `SLASHID_POLL_LAG_SECONDS` and the tick cadence. Emits one invocation **per newly-produced assistant turn**, skipping `client_asserted` history, `synthetic_marker` messages and `content_unavailable` turns — the last being a turn whose content the API will not return, with a `reason` of `not_captured`, `client_aborted`, `cmek_key_revoked`, `retention_elapsed` or `oversize`. It never appeared in this tenant, which has no retention policy in force, but any customer with finite retention produces them, and emitting one would create a contentless invocation. The schema also tells callers to tolerate unrecognized `type` values, so an unknown provenance is skipped rather than rejected. Fills `output`, `used_tools` and `stop_reason` — **inferred from block shape**, exactly as the hook path infers it, because no surface supplies it.

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
| `request_id` | content-addressed; the frame's own on a denial |
| `identity_details` | `{kind: "anthropic", user_id: actor.id}`. A null `actor.id` drops the event — the server rejects an identity with no identifier. |
| `timestamp` | the attested `webhook-timestamp`, or the message's `created_at` on a reader-emitted event |
| `conversation_id` | `session_id` |
| `model` | `AIModel(id=model or "unknown", provider="anthropic", raw_model_id=model)`. `shared.model_catalog` is Bedrock-only and unused. |
| `input` / `output` | per the completion predicate above |
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
│   ├── pending.py                       # the record, the completion predicate, the flush
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

Two things belong in the spine rather than either package: `content_request_id`, a pure function over `(first toolu id | None, session_id, ordinal, text)` so both sources compute byte-identical keys, and the event assembly they share. `CheckpointStore` should be promoted from `vertex/` into `shared/` rather than copied, and its `Checkpoint(timestamp, id)` type carries over unchanged: every compliance feed accepts a timestamp lower bound, so the same watermark works here. Two changes come with the move. The store must take a **cursor name**, because `vertex/` writes one fixed document and this service keeps three independent watermarks. And the watermark is persisted as a timestamp, never as one of the feeds' opaque page tokens, which the API documents as format-unstable; those tokens paginate within a tick and are then discarded.

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

Four shared additions: `AnthropicIdentityDetails` in the `IdentityDetails` union; `AnthropicToolUseBlock.name` gaining `AliasChoices("name", "tool_name")`; an `AnthropicAttachmentBlock` translating to `kind="document"`; and `AIAccessedFile.provenance`. `parse_media_type` also needs fixing — it claims to reject unregistered types but never does, so an unregistered `media_type` currently raises instead of falling back to `None`.

## Configuration

`Config(BaseConfig)` adds:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SLASHID_HOOK_SIGNING_SECRET` | unset | `whsec_…`; comma-separated accepts any number, tried in order. **Setting it enables the hook.** |
| `SLASHID_HOOK_ALLOW_UNSIGNED` | `false` | escape hatch for an org that enabled hooks before secrets were required; cannot be combined with `POLICY_URL` |
| `SLASHID_POLICY_URL` | unset | the `ng-evangelion` receiver's `/ai-access/<id>`; unset skips the policy check |
| `SLASHID_PREFLIGHT_ENABLED` | `false` | call `{ENDPOINT}/ip/nhi/ai/preflight`; keep off until that endpoint is deployed |
| `SLASHID_VERDICT_FAIL_MODE` | `allow` | `allow` or `deny` when a check fails or answers unverified |
| `SLASHID_VERDICT_BUDGET_MS` | `3500` | both checks, concurrently, under Anthropic's timeout |
| `SLASHID_SHADOW_MODE` | `true` | our own shadow mode, named after claude.ai's `shadow_mode` field and **independent of it**: when either is on, nothing is blocked. On by default, so a fresh deployment observes before it enforces. |
| `SLASHID_MAX_BODY_BYTES` | `33554432` | Cloud Run's HTTP/1 limit |
| `SLASHID_JOIN_WAIT_SECONDS` | `3600` | deadline before an unsettled record is pushed as it stands |
| `SLASHID_TOMBSTONE_TTL_SECONDS` | `7200` | how long a pushed record's tombstone suppresses a late reader's duplicate; must exceed `JOIN_WAIT` + `POLL_LAG` + one tick |
| `SLASHID_FIRESTORE_DATABASE` | `(default)` | the named database, as `vertex/` does |
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

**At least one credential must be present**, or startup fails. The signing secret is required only when the hook is in use, so compliance-only needs none.

**Two conflicts with the code already on the branch**, both to settle in the implementation plan rather than silently:

- `config.py` validates that a signing secret is present unless `HOOK_ALLOW_UNSIGNED` is set. That makes compliance-only impossible to start today. The validator has to become conditional on the hook capability being in use.
- `config.py` defaults `preflight_enabled` to `true`, against the `false` above. The table is the intent, since the preflight endpoint has not shipped; the code default flips.

## Tests

Fixture-driven, matching the house pattern, with `yaml_pytest` case tables over captured frames and recorded API responses.

- `test_signature.py` — valid, tampered, stale, future-dated, unsigned, malformed secret, several candidate signatures, re-cased headers, N secrets tried in order, and a secret whose base64 contains `+` and `/`.
- `test_frame.py` — unknown block type, unknown `source.application`, unknown `actor.type`, unknown top-level `type`, null `session_id`/`model`; the transcript split, including a consumed round spanning two user messages and a merged assistant run.
- `test_policy.py` / `test_preflight.py` — raw bytes and headers forwarded unchanged; deny with reason; non-200 and timeout raise; `overall` rows map to allow, deny and unverified; caps treated as unverified.
- `test_verdict.py` — both allow; each deny wins with its reason; transport failure and unverified honour the fail mode; budget exceeded; `config-test` and unknown type bypass; observe-only allows while still evaluating; the recovery sentence is appended; `reference_id` charset.
- `test_pending.py` — a record is written rather than pushed in every configuration, the hook alone included; the next frame settles a no-attachment round immediately; an attachment-bearing round waits; a reader's visit that finds no listing settles it too; **a record past the deadline is pushed, not deleted**; a flush carries whatever the record holds, including `output` a successor frame already supplied; a pushed record leaves a tombstone; a tool-free run flushes rather than joining on ordinal.
- `test_store.py` — against a fake and, when credentials allow, the Firestore emulator: `upsert` twice does not move the deadline; two concurrent `complete` calls both land, neither lost; `due` excludes tombstoned records and honours its bound; `seen` distinguishes live, tombstoned and absent; an oversized `input` is dropped rather than failing the write.
- `test_event_envelope.py` — content address anchors on `toolu_` and falls back with the `hook:` prefix; stability across two frames revealing one turn; consumption attribution; `cat -n` stripped; null `actor.id` drops.
- `test_client.py` — `clls_` decode yields the frame's `session_id`; synthetic and `client_asserted` messages are skipped; truncation surfaced; own `compliance_api_accessed` filtered; a session outside the bound organization skipped.
- `test_denials.py` / `test_responses.py` — an activity completes or emits standalone; `model` falls back; one event per blocked attempt; a newly-produced turn's content address is **byte-identical to the hook path's for the same run**.
- `test_attachments.py` — `md5` takes the listing digest and makes no request; `full` downloads and its md5 equals the listing's; a file over `MAX_ATTACHMENT_FETCH_BYTES` is never requested and keeps the listing's md5 alone; a listing with no md5 yields an entry with no digest; `attachment` entries are replaced and `tool_result` entries preserved.
- `test_main.py` — a sink failure still returns 200 with the correct verdict (**rule 1**); a slow sink does not delay the response; oversized body; unsigned gets 401.

## Deployment

Cloud Run, one Terraform module attached to the release, mirroring `vertex/deploy/terraform` in shape.

- The release workflow builds the image with `uv`, pushes it to GHCR, and the module pulls it through an Artifact Registry remote repository proxying `ghcr.io` (credentials required while the repo is private).
- **One service, two routes.** `POST /{path}` is the hook; `POST /tick` drives the readers and the deadline flush, fired by Cloud Scheduler with an OIDC token, concurrency 1 so checkpoint writes cannot race.
- **Hook only**: `min_instance_count = 1`, since a cold start inside the verdict budget risks a webhook failure and enough of those trip the circuit breaker. Needs the pending store and a slow tick to fire flushes; no compliance polling.
- **Compliance only**: no public endpoint, no certificate, no minimum instance — a scheduler, a checkpoint store and two secrets.
- Secret Manager holds the push token, the signing secret and the compliance key. Firestore holds the pending store and checkpoints, in a named database as `vertex/` does, behind the port above. Its TTL policy is a backstop only: flushing must emit, so it is the tick's job, not the TTL's, and the policy must never be set below `SLASHID_TOMBSTONE_TTL_SECONDS`. The module also provisions the composite index `due` needs.
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
- **The local-sessions listing cannot be ordered**, so Reader B re-walks its whole lagging window every tick instead of resuming from a cursor. Dedup absorbs the repeats; a long outage still means a long re-walk.
- **Cloud Run's 32 MiB body cap** is below the protocol's ceiling; observed frames peak at 1.86 MB.

## Open questions

1. **Does `actor.id` resolve against what the `anthropic` adapter stores for org members**, so `ResolveAIInvocationIdentity` succeeds on the first invocation rather than never?
2. **Should a seat-authenticated `actor.type: user` on an interactive surface set `HumanDriven`?** Decided yes; it needs a new `Reason` value server-side, batched with the schema sync.
3. **Should `reference_id` carry which check denied** — `slashid:preflight:…` against `slashid:policy:…` — now that the activity's own `request_id` makes it redundant as a join key? It would diverge from the Go receiver's recipe, so it is a deliberate decision rather than a tidy-up.
4. **When should `generated_files` be enriched?** Deferred, deliberately. They are field-identical to `files[]` apart from `created_at`, so the enrichment itself is nearly free, but the frame never reveals that a tool wrote a file. Covering them means waiting on every round from a file-capable surface instead of every attachment-bearing round, and that cost is not worth paying before the feature has a user. Revisit when a customer runs claude.ai file creation in anger. `provenance` reserves the `generated` value for that day and ships without it.
5. **Should artifact content be fetched into `output`?** It is the one way to close the artifact gap in Known limitations, at one request per artifact version. It wants its own knob, not a fold into file enrichment.

## References

- [Inference hooks overview](https://platform.claude.com/docs/en/manage-claude/inference-hooks), [endpoint protocol](https://platform.claude.com/docs/en/manage-claude/inference-hooks-endpoint), [configuration](https://platform.claude.com/docs/en/manage-claude/inference-hooks-configuration)
- [Compliance API setup](https://platform.claude.com/docs/en/manage-claude/compliance-api-access), [Activity Feed](https://platform.claude.com/docs/en/manage-claude/compliance-activity-feed), [session transcripts](https://platform.claude.com/docs/en/manage-claude/compliance-sessions), [chats, files and projects](https://platform.claude.com/docs/en/manage-claude/compliance-content-data)
- [Standard Webhooks](https://www.standardwebhooks.com/)
- `ng-evangelion`: [#7733](https://github.com/slashid/ng-evangelion/pull/7733) and `docs/superpowers/specs/2026-09-18-ai-preflight-endpoint-design.md`; `2026-09-13-ai-access-hooks-policy-design.md` and `backend/modules/detections/components/aiauthorization/README.md`
- This repo: `docs/superpowers/specs/2026-08-27-vertex-ai-forwarder-design.md`
