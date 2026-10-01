# Anthropic forwarder

Observes Claude Enterprise AI invocations and pushes `AIInvocationObservedV1`
events to SlashID. Two capabilities, each enabled by the presence of its
credential: an inline **inference hook** that also answers allow/deny, and a
scheduled **compliance reader**.

Deployed via `deploy/terraform/`; `deploy/dev-deploy.sh` is the iteration path
against a test tenant. Each observed invocation is normalized into an
`AIInvocationObservedV1` event and pushed to the SlashID NHI subgraph.

**Capabilities follow the credentials.** `SLASHID_HOOK_SIGNING_SECRET` enables
the hook, `SLASHID_COMPLIANCE_KEY` enables the readers, at least one is
required, and hook-only, compliance-only and both are configurations of one
image. Neither capability pushes from the request path: both write to a
Firestore pending store addressed by content, and a claim decides who pushes.

## Scope

- **Supported**: Claude Enterprise Inference hooks (`prompt` frames, with an
  allow/deny answer) and the Compliance API's activity, chat-message and
  local-session feeds.
- **Deferred**: claude.ai extended research (emits no frames at all), and any
  inventory of declared-but-unused tools (no surface carries one).

## Prerequisites

For the hook:

- Claude Enterprise, and `organization:manage` — Owner or Primary owner — to
  configure the Inference hooks endpoint.
- An `https://` endpoint on port 443, publicly routable, with a valid public CA
  certificate, no redirects and no reverse tunnels.

For the compliance readers:

- The Compliance API enabled **by the primary owner**. Enablement is not
  retroactive and cannot be backdated.
- A Compliance Access Key with `read:compliance_activities` and
  `read:compliance_user_data`.

For both:

- A SlashID push token for an **`anthropic`** connection. **One deployment, one
  token.** Splitting the hook and the readers across two deployments is not
  supported: two deployments cannot share a pending store, so the join between
  the sources disappears. Content addressing still makes both halves compute the
  same `request_id`, so a *shared* token leaves the terminal's dedup to collapse
  the overlap and only the enrichment is lost — but **separate connections**
  make the dedup key `{org}:{conn}:{request_id}` differ, and every invocation
  both halves saw is counted twice.

## Known limitations

Measured against a live tenant, not anticipated. Not bugs — gotchas to plan
around. Extend as new ones surface.

- **Two sources can only be joined on a `tool_use` id.** Transcript-prefix
  digests, measured on one session present in both feeds, produced 200 keys
  from the frames and 302 from the reader with **zero in common**: the stored
  transcript is a different projection of the conversation — a prepended
  synthetic marker, turns from before capture was enabled, sub-agent turns.
  Only the model-minted `tool_use.id` survives both. So a run with no tool call
  is **owned by the hook alone**, a run the hook never saw is **never
  recorded**, and the tool-bearing runs — the ones that touch files and servers
  — are covered twice. Under a rollout percentage below 100 with no compliance
  key, the unsampled turns are simply absent.
- **Attachment digests reach fewer rounds than attachments.** Enrichment needs
  a joinable run, and only **6 of 14** measured claude.ai rounds had one. The
  other 8 keep the frame's extracted-text digest: exact for plain text, absent
  for a processed document.
- **A digest is of what Claude stored, not always of what was uploaded.** A
  measured image came back 2 KB larger as a processed copy, and some documents
  are stored as extracted text. Such a hash will not match the original file,
  and nothing marks which is which.
- **There is no metadata probe for an attachment.** `HEAD` on the file content
  endpoint 404s on every attachment, so a file's size and digest can be learned
  only from the listing that names it. That is why
  `SLASHID_MAX_ATTACHMENT_FETCH_BYTES` is decided from the listing's
  `size_bytes` *before* any fetch rather than from the file, and why `full`
  hashing depends on having walked the right listing first. A ranged read
  yields a snippet, never a digest.
- **Denials are sticky.** Under enforcement the denied content stays in the
  transcript and keeps being denied: the verdict scans the round after the last
  assistant message, a denial prevents an assistant message, so the offending
  block stays in scope and **every later turn in that session is denied**,
  however innocuous. The session is unrecoverable; only a new conversation
  escapes, which is why `deny_reason` says so. One incident therefore emits one
  denial event per subsequent turn — group them on `conversation_id` plus the
  `accessed_files` digests.
- **Shadow mode and blocking mode are indistinguishable on the wire.** No
  `is_shadow` field exists, and nothing across a 200-frame window spanning both
  settings infers one, so the receiver cannot tell whether the verdict it is
  about to return will be honoured. Denial activities compound it: they exist
  only for denials that were actually *honoured*, so **a shadow deployment
  produces no record of what it would have blocked** — which is the one thing an
  operator staging a rollout wants to read. The only record of it is ours:
  `composed_verdict` is stored on every event beside the `verdict` that went
  back, and the gap between the two is the answer to "what would this have
  blocked?". Read the Rollout section below with that in mind.
- **Compliance API enablement is not retroactive.** Nothing that happened
  before the primary owner enabled it is recorded, ever. There is no backfill.
- **No tool or MCP-server inventory.** `available_tools` lists only the tools
  actually *used*, by name, with no description and no schema. The Bedrock and
  Vertex forwarders read real declarations from the request body, so absence
  here means unobservable, not unused. On a frame an MCP server is visible only
  when the client names its tools `mcp__server__tool`; on claude.ai chat
  messages the reader does better, since tool blocks carry `integration_name`
  and `mcp_server_url` as explicit fields. Server attribution is partly
  recoverable on one surface, tool definitions on neither.
- **No `stop_reason` and no token counts** from any surface. `stop_reason` is
  inferred from the shape of the run; `tokens` is zero.
- **Server-tool results are placeholders**, so content Anthropic's own tools
  fetch is outside every check, and nothing marks a call server-executed.
- **claude.ai's extended research emits no frames**, so an agentic task's
  fetches are entirely uninspected.
- **Hash matching from a frame covers plain text only.** The reader closes this
  for files Claude stores intact — subject to the two limits above.
- **`conversation_id` merges sub-conversations.** Haiku status frames and
  `web_search` sub-requests share the main session's id.
- **Reader-emitted events are 10 KB-capped per tool block**, so a frame-built
  record is the better-hashed one wherever both exist.
- **The local-sessions listing cannot be ordered**, so the response reader
  re-walks its whole lagging window every tick instead of resuming from a
  cursor. Dedup absorbs the repeats; a long outage still means a long re-walk.
- **A turn that becomes visible later than the tombstone TTL is dropped.** A
  conversation is re-read whole whenever it changes, so the response reader
  skips turns older than `SLASHID_TOMBSTONE_TTL_SECONDS` (2h): their
  tombstones may be gone, and they would otherwise be emitted again. A local
  session uploaded more than that after it happened loses those turns. The
  tick counter `responses_max_first_seen_lag_s` measures how late turns
  actually arrive; raise the TTL if it approaches the limit.
- **Cloud Run caps HTTP/1 bodies at 32 MiB**, below the protocol's 64 MiB
  ceiling. Observed frames peak at 1.86 MB.
- **Ticks overlap.** Cloud Run gives a second concurrent `POST /tick` a second
  instance, and per-instance concurrency does not serialize it. The tick takes a
  Firestore lease and exits if another holds it; that lease, not any Cloud Run
  setting, is what makes overlap safe.

## What we wish the provider gave us

Everything below is a limitation we measured rather than inferred, with the
measurement that produced it. Each one costs coverage, latency or code we would
not otherwise write, and each is cheap for Anthropic to close.

### 1. One identifier for an invocation, across all three surfaces

The single most expensive gap. Today the only value common to a hook frame and a
compliance transcript is a `tool_use.id` the model happened to mint, so an
invocation is joinable **only if it used a tool**: 194 of 284 trailing runs, and
just 6 of 14 on claude.ai, which is where attachments live.

Nothing else survives the crossing. A digest over the transcript prefix produced
200 keys on the frame side and 302 on the reader side with **zero in common** —
unchanged after dropping `synthetic_marker` messages, and still zero with every
text block removed so only roles and block ids contributed. The stored
transcript is a different projection: it prepends a marker the client never
sent, carries turns from before recording began, and includes sub-agent turns no
frame shows.

There is also nothing to key on *before* the model answers. A frame's messages
carry only `role` and `content`, with no message id, and the frame's own
`request_id` is just the delivery id, distinct on all 492 captured deliveries.

**What would fix it:** a stable invocation id on the prompt frame that also
appears on the corresponding assistant turn in the transcript and on the denial
activity. One field, and the join goes from 68% to exact.

### 2. Ordering on the local-sessions listing

`/apps/sessions/local` rejects both `order` and `order_by` and returns
newest-first. A poller therefore cannot resume from a cursor: it must drain the
entire lagging window every tick and rely on its own tombstones to suppress what
it already emitted. A bound on sessions per tick then risks truncating the
*oldest* tail, so the watermark cannot advance on a partial drain, and a backlog
can grow without bound.

The two neighbouring feeds do support it, with three different vocabularies:
activities takes `order=asc`, chats takes `order_by=updated_at` and *rejects* an
`updated_at` filter unless it is present, sessions takes neither. Page cursors
differ too — `last_id` on two feeds, `next_page` on the third.

**What would fix it:** `order_by=updated_at` with ascending order on the session
listing, and one pagination vocabulary across the three feeds.

### 3. `stop_reason` and token usage, anywhere

No surface carries either. Every event this forwarder emits infers `stop_reason`
from block shape and reports zero tokens. For a product whose job is to account
for AI usage, the usage numbers are simply absent.

### 4. An enforcement-mode indicator on the frame

Shadow mode and blocking mode are structurally identical on the wire: across a
200-frame window spanning both settings there is no `is_shadow` field and
nothing to infer one from. A receiver cannot tell whether the verdict it is
about to return will be honoured, so "what would this have blocked?" rests on an
operator assertion the receiver cannot verify.

Denial activities compound it: they exist only for denials that were actually
honoured, so a shadow-mode deployment produces no record of what it would have
blocked — exactly the audit surface an operator evaluating a rollout needs.

### 5. Tool and MCP-server declarations on the frame

The frame carries no tool definitions, so `available_tools` can only be
synthesized from the names of tools actually **used**: no description, no
schema, and every declared-but-unused tool invisible. An MCP server surfaces
only when a client happens to name its tools `mcp__server__tool`. The Bedrock
and Vertex forwarders read real declarations from the request body, so absence
here means unobservable rather than unused.

Chat transcripts do better — tool blocks there carry `integration_name` and
`mcp_server_url` — which shows the metadata exists and simply does not reach the
inline surface.

### 6. Untruncated content for a compliance reader

Transcript tool blocks are capped at 10,000 bytes by default. Raising the cap is
possible per request, but the consequence is structural: a reader-built event is
always worse-hashed than a frame-built one, and no content-derived value can be
compared across the two surfaces.

### 7. A metadata probe for attachments

`HEAD` on the file content endpoint 404s on every attachment, so there is no
cheap way to learn a file's size or digest before deciding whether to download
it. The listing carries both, which works, but it means size-capped fetching
depends on having walked the right listing rather than on asking about the file.

### 8. Recording that covers what already happened

Compliance recording is not retroactive: nothing before enablement is recorded,
ever. An organization turning the API on has no way to audit the conversations
that motivated turning it on.

### Smaller ones

- **Server-tool results arrive as `[non-text content]` placeholders**, so
  content Anthropic's own tools fetch is outside every check, and nothing marks
  a call server-executed.
- **claude.ai's extended research emits no frames at all**, so an agentic task's
  fetches are entirely uninspected.
- **`files`, `generated_files` and `artifacts` exist only on chat messages.** A
  Claude Code session message carries none of them, so a file written by a
  Claude Code run is visible only as whatever its tools echoed.
- **A run means two different things on the two sources.** In a frame a tool
  result arrives in the following user message; in a chat transcript the whole
  cycle sits inside one assistant message. Two walks, one concept.

## Development

```bash
(cd anthropic && uv run pytest)   # fake Firestore client, recorded API responses
```

No emulator and no credentials: the suite runs entirely against doubles and the
recorded corpus under `tests/fixtures/`.

Live iteration against a test tenant, with frame capture on:

```bash
(cd anthropic && ./deploy/dev-deploy.sh PROJECT [REGION])
```

## Two failure modes, two owners

`SLASHID_VERDICT_FAIL_MODE` covers a check that fails or answers unverified.
Anthropic's own failure handling covers the case where this service does not
answer at all. They are different settings on different sides, and both are
needed for a strict deployment.

`SLASHID_SHADOW_MODE` is ours; claude.ai's `shadow_mode` is theirs. **When
either is on, nothing is blocked.**

## The eventing path never affects a verdict

The push runs after the response has gone out, bounded by
`SLASHID_PUSH_BUDGET_MS`. That budget is what protects the instance, which
makes the sink's own `SLASHID_REQUEST_TIMEOUT_SECONDS` and `SLASHID_MAX_RETRIES`
largely inert — they can only spend time the push budget has already capped.

## Configuration

All env vars use the `SLASHID_` prefix, except `LOG_LEVEL`. Rows marked
*container-env-only* are not Terraform variables: set them on the revision.

| var | required | default |
| --- | --- | --- |
| `SLASHID_ENDPOINT` | yes | — |
| `SLASHID_PUSH_TOKEN` | yes | — |
| `SLASHID_PLATFORM` | no | `gcp` (the only one today) |
| `SLASHID_PROJECT_ID` | yes | — |
| `SLASHID_HOOK_SIGNING_SECRET` | one capability required | — (comma-separated; any number live during a rotation) |
| `SLASHID_COMPLIANCE_KEY` | one capability required | — |
| `SLASHID_ORGANIZATION_UUID` | with `SLASHID_COMPLIANCE_KEY` | — |
| `SLASHID_PREFLIGHT_ENABLED` | no | `false` |
| `SLASHID_VERDICT_FAIL_MODE` | no | `allow` |
| `SLASHID_VERDICT_BUDGET_MS` | no | `3500` |
| `SLASHID_PUSH_BUDGET_MS` | no | `2000` |
| `SLASHID_SHADOW_MODE` | no | `true` |
| `SLASHID_MAX_BODY_BYTES` | no | `33554432` (Cloud Run's HTTP/1 cap) |
| `SLASHID_DATABASE` | no | `slashid-anthropic` |
| `SLASHID_PENDING_COLLECTION` | no | `anthropic_pending` |
| `SLASHID_CHECKPOINT_COLLECTION` | no | `anthropic_checkpoints` |
| `SLASHID_JOIN_WAIT_SECONDS` | no | `3600` |
| `SLASHID_TOMBSTONE_TTL_SECONDS` | no | `7200` — must exceed `JOIN_WAIT + POLL_LAG + TICK_INTERVAL`, asserted at startup |
| `SLASHID_MAX_FLUSHES_PER_TICK` | no | `500` |
| `SLASHID_TICK_INTERVAL_SECONDS` | no | `300` |
| `SLASHID_TICK_PRINCIPAL` | no | — (unset refuses every tick) |
| `SLASHID_TICK_AUDIENCE` | no | — (Cloud Run checks it where ingress is not public) |
| `SLASHID_POLL_LAG_SECONDS` | no | `120` |
| `SLASHID_MAX_SESSIONS_PER_TICK` | no | `200` |
| `SLASHID_ATTACHMENT_HASHING` | no | `md5` (or `full`) |
| `SLASHID_MAX_ATTACHMENT_FETCH_BYTES` | no | `10485760` |
| `SLASHID_COMPLIANCE_TIMEOUT_SECONDS` | no | `60` (per Compliance API request) |
| `SLASHID_RESPONSE_READER_BUDGET_SECONDS` | no | `300` (the response reader's share of a tick) |
| `SLASHID_MAX_TRANSCRIPT_MESSAGES` | no | `2000` (longer sessions emit from their tail) |
| `SLASHID_SOFT_JOIN_WINDOW_SECONDS` | no | `15` |
| `SLASHID_INCLUDE_RAW_CONTENT` | no | `false` |
| `SLASHID_MAX_CONTENT_SIZE` | no | `100000` |
| `SLASHID_INPUT_SCOPE` | no | `round` (the messages since the last response; `session` sends the whole transcript) |
| `SLASHID_ROUND_LINK_DEPTH` | no | `10` (rounds listed in `recent_round_hashes`) |
| `SLASHID_REQUEST_TIMEOUT_SECONDS` | no | `10.0` — *container-env-only* |
| `SLASHID_MAX_RETRIES` | no | `3` — *container-env-only* |
| `SLASHID_CAPTURE_BUCKET` | no | — *container-env-only*, test tenants |
| `SLASHID_CAPTURE_DENY_MARKER` | no | — *container-env-only*, test tenants |
| `SLASHID_MOCK_DENIED_HASHES` | no | — *container-env-only*, test tenants |
| `LOG_LEVEL` | no | `INFO` |

`SLASHID_MOCK_DENIED_HASHES` denies any invocation whose `accessed_files` carry
a listed digest. It runs **alongside** preflight rather than instead of it. It
exists so a test tenant can drive a real denial without depending on the graph
having anything tagged sensitive: the deny reason, the guardrail stamp, the
`deny:` address and the denial reader's join onto it. Content-addressed, unlike
`SLASHID_CAPTURE_DENY_MARKER`: a literal token is tripped by anyone who merely
quotes it.

## Rollout

Anthropic provides staged rollout server-side — shadow mode, a rollout
percentage, role exclusions, then enforcement with your choice of fail-open or
fail-closed. Use it, and ship `SLASHID_SHADOW_MODE=true` so nothing is blocked
until the customer opts in.

With a compliance credential present, a low rollout percentage stops being a
coverage decision and becomes purely an enforcement one: turns the hook never
saw are still emitted by the reader — subject to the `tool_use`-only join above.

## Release

```bash
git tag anthropic-v0.1.0 && git push origin anthropic-v0.1.0
```

publishes `ghcr.io/slashid/slashid-anthropic-forwarder:0.1.0`. The tag must
match the `version` in `anthropic/pyproject.toml`; the release workflow refuses
the mismatch.
