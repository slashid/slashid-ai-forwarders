# Anthropic forwarder

Observes Claude Enterprise AI invocations and pushes `AIInvocationObservedV1`
events to SlashID. Two capabilities, each enabled by the presence of its
credential: an inline **inference hook** that also answers allow/deny, and a
scheduled **compliance reader**.

The rest of this README is written as the implementation lands. What follows is
the part that is already worth writing down.

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
