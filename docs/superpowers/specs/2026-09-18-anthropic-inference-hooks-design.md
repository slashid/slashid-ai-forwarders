# Claude Inference Hooks — AI invocation collection with inline enforcement

**Date:** 2026-09-18 (revised 2026-09-20; see [Revision notes](#revision-notes))
**Status:** Design approved for implementation. Wire shapes, sizing and **enforced denial** verified against a live Claude Enterprise tenant on 2026-09-20 (see [Observed wire shapes](#observed-wire-shapes) and [Sticky denials](#sticky-denials)). Seven of the eight verification items are now answered or decided; only item 8 is open. **The architecture was revised after that run: see [Two sources, three deployments](#two-sources-three-deployments), which supersedes the inline-only design described in the sections before it.**
**Design hub:** this repo (`mcp-agent`) — draft/design/POC surface, unchanged.
**Target repo:** `slashid-ai-forwarders`, new subdirectory `anthropic/`.
**Siblings in target repo:** `bedrock/`, `vertex/`, `shared/`.
**Companion change:** `POST /ip/nhi/ai/preflight` in `ng-evangelion` ([PR #7733](https://github.com/slashid/ng-evangelion/pull/7733)).
**Chains to:** the existing Go Claude-hook policy receiver in `ng-evangelion` (`backend/modules/detections/components/aiauthorization`, `POST /ai-access/<id>`).

## Overview

An HTTPS service that receives [Claude Enterprise Inference hooks](https://platform.claude.com/docs/en/manage-claude/inference-hooks) — Anthropic's inline pre-inference webhook — and does two independent things with each frame: returns an allow/deny verdict before the model runs, and pushes an `AIInvocationObservedV1` to SlashID's NHI subgraph.

Unlike `bedrock/` and `vertex/`, which are triggered by an audit-log pipeline and may take their time, this receiver is **inbound, synchronous, and in a user's critical path**. That single difference drives most of the design: the verdict is latency-bound and must never be coupled to the success of event delivery.

```
user submits prompt / a tool result returns
        │
        ▼
 Anthropic servers ──POST (signed)──► anthropic/ receiver (Cloud Run, FastAPI)
        ▲                                   │
        │                                   ├─► POST /ai-access/<id> (raw frame, forwarded)  ──► SlashID policy
        │                                   ├─► POST /ip/nhi/ai/preflight (hashes)            ──► SlashID graph
        │   {"action":"allow"}               │        verdict = AND, blocking, under budget
        └───{"action":"deny", …}◄────────────┤
        │                                   └─► POST /nhi/events/ai-invocations              ──► SlashID
        ▼                                          (previous invocation, best-effort)
 inference proceeds, or is rejected
```

The receiver is **stateless**. No session store, no checkpoint, no sticky routing — every response is a pure function of the frame in hand. It therefore runs unchanged in SlashID's cloud or in a customer's GCP account. The compliance readers added later keep polling checkpoints, but they are a separate component and the receiver never consults them.

## Why this surface

Neither Anthropic nor OpenAI exposes per-invocation content for API-key workloads, and their post-hoc audit surfaces cannot block. Verified coverage:

| Surface | Mechanism | Content | Real-time | Can block | User identity |
| --- | --- | --- | --- | --- | --- |
| Anthropic Activity Feed | pull | no | <1 min | no | yes |
| Anthropic Compliance API — local sessions | pull | yes, 10 KB-truncated | minutes | no | yes |
| **Anthropic Inference hooks** | **push** | **yes, untruncated** | **inline** | **yes** | **yes** |
| OpenAI Compliance Logs Platform | pull | yes | minutes | no | yes |
| Anthropic/OpenAI API-key workloads | — | **none** | — | no | — |

Inference hooks are the only surface on either provider that satisfies all three delivery requirements: no client-side configuration, attributable to a named user, and able to stop a request before the model sees it.

Critically, hooks fire on **both** the opening prompt and each returning tool result (the [overview](https://platform.claude.com/docs/en/manage-claude/inference-hooks) flow diagram hooks steps 1 and 6), because a tool result going back into the model is itself an inference call. So a `Read` of a sensitive file is inspected *before its contents reach the model* — which is exactly the file-exfiltration control we want, at the only moment it can be enforced.

## Why one receiver chains to another

`ng-evangelion` already ships a Claude-hook receiver: `aiauthorization` (landed in [#7649](https://github.com/slashid/ng-evangelion/pull/7649), spec `2026-09-13-ai-access-hooks-policy-design.md`). Bound per deployment through `AI_ACCESS_CONFIG_FILE`, it verifies the Standard Webhooks signature, requires an exact `tenant_id`, resolves `actor.id` through the identity graph and evaluates graph-scoped policies (model allowlists, time windows, attachment use). It emits no invocation events and matches no content hashes.

A Claude organization configures exactly one hook URL. So the forwarder is that URL, and it **chains**: it forwards the frame to the policy receiver for the graph-policy verdict, asks preflight about the content, ANDs the answers, and owns eventing. Each side keeps one job — the Go receiver decides *who may do what*, preflight decides *whether this content may be shown*, the forwarder observes and composes. The alternative — teaching the Go receiver to emit events and hash content — would put eventing inside the UM service and give up the customer-hosted deployment.

## Goals

1. Emit `AIInvocationObservedV1` events for Claude Code, Cowork and claude.ai activity, attributed to a named Claude Enterprise user, with no software or settings file on the endpoint.
2. Deny, inline, any governed request whose newly-arriving content matches a file tagged sensitive in the customer's own graph, or that the organization's graph policy denies.
3. Keep the receiver stateless, so it can be hosted anywhere and scaled horizontally; confine all checkpointing to the pull readers.
4. Reuse `shared/` for normalization, hashing, batching and delivery; add only what is genuinely hook-specific.
5. Never let a failure in the SlashID event path degrade into a blocked user.

## Non-goals

- **Response-side enforcement.** Anthropic's only hook event today is `prompt`; *"response-side enforcement is planned as a later event."* We handle the forward-compatibility contract and nothing more.
- **Prompt mutation / redaction.** Verdicts are allow or deny; rewriting is not supported by the protocol.
- **Tool and MCP-server inventory.** No frame carries tool definitions, and no other Anthropic surface exposes them, so the receiver cannot report what an invocation *could* have called, only what it did. A policy of the form "this identity may not use that MCP server" is therefore unenforceable here before the fact; it can only be observed after a call to that server appears.
- **Agentic sub-task traffic.** claude.ai's extended research task and similar background agents emit no frames at all (observed), so their fetches are outside every control here. Only the launching turn and the returned result are governed.
- **Policy evaluation in the forwarder.** Graph policy lives in the Go receiver; the forwarder never interprets policy, only its verdict.
- **OpenAI.** No inline hook surface exists. Its Compliance Logs Platform is a separate, later pull collector.
- **Bedrock / Vertex-hosted Claude.** Inference hooks are explicitly unavailable there; `bedrock/` and `vertex/` already cover those paths.
- **API-platform (Claude Console) traffic.** *"Platform organizations (API access through the Claude Platform) are out of scope"* for hooks, and the Compliance API exposes no content for API-key workloads either.
- **Token and cost accounting.** The frame carries no `usage` block at all. Tokens stay with the Claude Enterprise Analytics API, which the `anthropic` NHI adapter already pulls.
- ~~Compliance API session-transcript pull.~~ **No longer a non-goal.** It is now a first-class source and a shippable deployment on its own; see [Two sources, three deployments](#two-sources-three-deployments).

## Prerequisites (customer-side)

1. **Claude Enterprise.** Inference hooks are Enterprise-only, and configuring them needs `organization:manage` — Owner or Primary owner.
2. **A reachable endpoint.** `https://` on port 443, publicly routable (private, loopback and CGNAT ranges are refused at connect time), a certificate valid against the public CA trust store, and no redirects. Reverse tunnels (ngrok and similar) are blocked by Anthropic's network policy. A Cloud Run `*.run.app` URL satisfies all of this without a custom domain.
3. **A signing secret,** generated by the customer during setup. Enabling hooks requires one. The same secret is configured on the Go policy receiver, which re-verifies the forwarded frame.
4. **A SlashID push token** — the `anthropic` connection's event-streaming bearer credential, same as the other forwarders. The connection must be an **Anthropic** connection: the server picks the identity resolver from the connection's source type.

## Prerequisites (SlashID-side, outside this design)

- **A gate route for `/ai-access/<id>`.** The Go handlers are mounted directly on the UM echo server and have no `deploy/terraform/gate.hcl` route today, so they are not reachable through `api.slashid.com`. Until one exists, `SLASHID_POLICY_URL` stays unset and the policy check is skipped.
- **An `AI_ACCESS_CONFIG_FILE` endpoint** of kind `claude_hook` bound to the tenant, with the same signing secret.
- **PR #7733 merged and deployed.** Until then deploy with `SLASHID_PREFLIGHT_ENABLED=false`; left enabled, preflight answers 404, which the forwarder treats as a transport failure under its fail mode on every frame.

## Architecture

### Module layout

One module, a package per source, and a shared spine between them:

```
anthropic/
├── pyproject.toml                       # uv workspace member; depends on ../shared
├── Dockerfile                           # uv-built image, uvicorn entrypoint
├── README.md
├── src/slashid_anthropic_forwarder/
│   ├── __init__.py
│   ├── config.py                        # one Config; capabilities derived from the credentials present
│   ├── main.py                          # FastAPI: POST /{path} is the hook, POST /tick is the reader
│   ├── pending.py                       # the join store; only used when both credentials are set
│   ├── hook/
│   │   ├── signature.py                 # wrapper over the standardwebhooks library
│   │   ├── frame.py                     # PromptFrame envelope + split_transcript; messages reuse shared/normalize/anthropic
│   │   ├── capture.py                   # raw-frame capture, test tenants only
│   │   ├── checks.py                    # Verdict, CheckFailed — shared by the two checks and the composer
│   │   ├── policy.py                    # forward the raw frame to the Go receiver
│   │   ├── preflight.py                 # POST /ip/nhi/ai/preflight
│   │   ├── verdict.py                   # compose policy + preflight; owns fail mode + verdict budget (rule 2)
│   │   └── event_envelope.py            # frame → AIInvocationObservedV1, and the pending record
│   └── compliance/
│       ├── client.py                    # activity feed, session listing (clls_ decode), transcripts, chats, file content
│       ├── checkpoint.py                # two watermarks: the activity cursor, the lagging updated_at bound
│       ├── attachments.py               # download + digest, and the size_bytes-mismatch signal
│       ├── denials.py                   # Reader A
│       └── responses.py                 # Reader B
├── tests/
│   ├── fixtures/                        # captured frames, and recorded compliance responses
│   ├── test_config.py test_signature.py test_frame.py
│   ├── test_policy.py test_preflight.py test_verdict.py
│   ├── test_event_envelope.py test_main.py
│   ├── test_pending.py
│   └── test_client.py test_denials.py test_responses.py test_attachments.py
└── deploy/terraform/
```

One package, one image, one Terraform module; which halves run is decided by configuration rather than by deployment. Two things belong in the spine rather than in either package: `content_request_id`, as a pure function over `(first toolu id | None, session_id, ordinal, text)` so both sources compute byte-identical keys, and the event assembly they share. `CheckpointStore` should be promoted from `vertex/` into `shared/` rather than copied, since a second forwarder now needs it.

### Reused from `shared/`

The normalization and delivery machinery already exists; this is most of why the target repo was chosen. Three shared changes: `events.py` gains `AnthropicIdentityDetails` in the `IdentityDetails` union; `AnthropicToolUseBlock.name` gains `validation_alias=AliasChoices("name", "tool_name")` (the hook spells it `tool_name`, the Messages API `name`; pydantic 2.13 accepts either on one field); and an `AnthropicAttachmentBlock` (`file_name`, `media_type`, `size_bytes`, `text`, all nullable) joins the request-side union and translates to `kind="document"`. `AnthropicToolResultBlock` already fits: the hook's extra `tool_name` is ignored. Everything else is used as-is.

| Module | Used for |
| --- | --- |
| `normalize/turn.py` → `after_last_assistant()` | the fresh-round split that drives the verdict scan; also used internally by the shared helpers below |
| `normalize/anthropic/schema.py` → `AnthropicRequestMessage`, `AnthropicRequestBody`, `AnthropicMessage` | the frame's `messages` parse as `list[AnthropicRequestMessage]` directly; two additions, below |
| `normalize/anthropic/normalize.py` → `message_to_normalized_invocation()` | the whole event-side translation: request = transcript before `A`, response = `A` synthesized as an `AnthropicMessage` |
| `normalize/normalized/types.py` | `NormalizedInvocation` / `NormalizedMessage` / `NormalizedContent`, the intermediate the envelope builder consumes |
| `normalize/finalize.py` → `finalize()` | `accessed_files` from `Read`-style tool results in the consumed round, `cat -n` stripped, multi-algorithm hashed, deduped by name+hash |
| `events.py` → `build_event_from_normalized()`, `EventEnvelope` | the event itself: content hashing of `input`/`output`, `used_tools`, `stop_reason` |
| `normalize/normalized/tools.py` → `build_tools_declared()` | synthesizes `available_tools` / `available_tool_servers` from observed tool names, so `_used_tools` can resolve ids |
| `normalize/normalized/tool_results.py` → `extract_tool_result_files()` | the same extractor, called directly for the preflight hash set. It must be handed the **full** message list: it builds its `tool_use` index from the assistant messages and selects the fresh round itself with `after_last_assistant`. Handed only `U_new` it finds no `tool_use` and hashes nothing. |
| `content_utils.py` → `strip_cat_n`, `truncate_middle` | via the above |
| `sink.py` | `POST /nhi/events/ai-invocations`, wire-byte batching, retry classification (`TransientPushError` / `PermanentPushError`), `_redact_content` |
| `config_base.py` | `SLASHID_ENDPOINT` / `_PUSH_TOKEN` / `_INCLUDE_RAW_CONTENT` / `_MAX_CONTENT_SIZE` |
| `events.py` | `AIInvocationObservedV1` and the rest of the wire models |

New code is therefore limited to: signature verification, the frame envelope, the two check clients, verdict composition, the FastAPI entrypoint, config, Dockerfile and Terraform, plus the three small additions to `shared/`.

### The prompt frame

Body fields, per the [protocol reference](https://platform.claude.com/docs/en/manage-claude/inference-hooks-endpoint):

| Field | Notes |
| --- | --- |
| `type` | `"prompt"` today. **Unknown values must return `allow`**, never an error. |
| `request_id` | per-inference-call id; equals the `webhook-id` header |
| `tenant_id` | opaque organization id, nullable |
| `actor` | union on `type` (`"user"` only today): `id` (`user_01…`), `email_address`; both nullable |
| `source.application` | open string — `claude-ai`, `claude-code`, `cowork`, `config-test`. **Advisory routing metadata, not a trust boundary.** |
| `messages` | cumulative transcript up to the point of inference, including prior turns |
| `session_id` | opaque conversation id, nullable; for Claude Code *"best-effort, client-asserted"* |
| `model` | public model id, nullable |
| `metadata` | reserved, empty today; tolerate any keys |

Content blocks: `text{text}`, `tool_use{id, tool_name, input}`, `tool_result{content, is_error, tool_name, tool_use_id}`, `attachment{file_name, media_type, size_bytes, text}`. Blocks of unrecognized `type` must be skipped, never rejected.

Absent by design: system prompts, tool definitions, thinking blocks, raw file/image bytes, and any token usage. Tool results appear under the **`user`** role, matching the Messages API content model, and *"a turn whose every block is excluded is omitted entirely, so don't assume strict user and assistant alternation."*

Transcripts are sent **untruncated** — typically under 10 MB, up to 64 MiB by protocol. Body-size limits must be raised deliberately: a rejected body is a webhook failure, and under *allow* failure handling an oversized prompt would reach the model uninspected.

Translation to the canonical shape is the shared normalizer's: `text` → `kind="text"`; `tool_use` → `kind="tool_use"` with `tool_use_id`, `tool_name`, `tool_input`; `tool_result` → `kind="tool_result"` with `tool_use_id`, `tool_output`, `tool_is_error`; unknown → skipped (`AnthropicUnknownBlock`). The one addition, `attachment` → `kind="document"` with `text`, `media_type`, `byte_length=len(text.encode())` (the same bytes `attachment_files` hashes, not the frame's `size_bytes`). `NormalizedContent.media_type` is a validated `MimeType`, while the frame's `media_type` is an open, nullable string, so an unregistered value falls back to `None` rather than failing the frame. The `tool_name` on a `tool_result` is the hook protocol's convenience and is not needed once the join on `tool_use_id` exists.

### Signature verification

Standard Webhooks: `webhook-id`, `webhook-timestamp`, `webhook-signature`. HMAC-SHA256 over `{webhook-id}.{webhook-timestamp}.{raw body bytes}`, compared constant-time against each space-separated `v1,<base64>` candidate.

**The crypto is delegated to the `standardwebhooks` reference library** rather than hand-rolled. It is the implementation of the specification Anthropic follows, carries no dependencies of its own, and was checked against this design's own cases: valid, tampered, stale, future-dated, unsigned, malformed secret, multiple candidate signatures, re-cased headers, and a secret whose base64 contains `+` and `/`. That last one is the trap the protocol documentation warns about — a URL-safe decoder derives the wrong key whenever the secret contains those characters, which is most of the time — and delegating puts it permanently out of our hands.

Two things the library does not do, which is all the wrapper is for: it takes one secret per instance where a rotation needs two, and it raises where the caller wants a bool. It also parses the body as JSON by default, which is turned off — at the megabyte frame sizes observed, that is a parse whose result would be discarded.

Two obligations stay with the caller. **Hash the raw bytes**, before any parse or re-encode, and **reject unsigned requests.** During a secret rotation, accept both secrets — requests signed with the previous one arrive for about a minute afterwards, plus anything in flight. Header names arrive lowercase but proxies may re-case them: look them up case-insensitively.

Allowlist Anthropic's egress block `160.79.106.0/24` as defence in depth, never as a substitute for verification — that block carries Anthropic egress beyond hooks.

### One transcript walk, two jobs

Because the frame is cumulative, frame N+1 contains everything invocation N needs; the receiver never has to remember frame N. Reading the tail:

```
messages = [ … , U_prev , A , U_new ]
                    │      │     └── newest user-role run → what the VERDICT scans
                    │      └──────── last assistant run   → invocation N's OUTPUT
                    └─────────────── everything before A  → invocation N's INPUT
```

**Verdict path.** Scan `U_new` — the file-shaped blocks that have *not yet* reached the model: `tool_result.content` and `attachment.text`. Hash them with `accessed_files_for(messages)` (below), send them to preflight, and forward the whole frame to the policy receiver. User `text` is not hashed: preflight matches files, and the policy receiver reads the raw frame.

**`accessed_files_for(frame_messages)`** in `event_envelope.py` is the one helper both paths use, so their hashes cannot diverge. It takes the frame's own `Message` list — `file_name` does not survive translation, since `NormalizedContent` has no name field — and is two functions: `attachment_files(frame_messages)`, one `AIAccessedFile` per `attachment` block in `after_last_assistant(frame_messages)` that carries `text`, with `name=file_name` (or the basename of the matching `<uploaded_files>` path when the block's name is null), `media_type`, `byte_length=len(text.encode())` and three digests over those same bytes (the original `size_bytes` is not used, so hash and length describe one byte string, as the extractor's do), no cleanup; plus `extract_tool_result_files(translate(frame_messages), config=config)` — handed the full translated list, with `attachment_files` honouring the same `include_raw_content` gating for `redacted_content` — it builds its `tool_use` index from the assistant messages and selects the fresh round itself. That mirrors how the Converse normalizer adds `extract_attachments` before `finalize`. The verdict path calls the full helper on the whole frame (fresh round = `U_new`), from `main.py`, which hands the result to `verdict.py`; `verdict.py` never imports `event_envelope.py`. The event path calls only `attachment_files` on the transcript before `A` (fresh round = `U_prev`), seeds `normalized.accessed_files` with it, and lets `finalize` add the tool-result files and dedupe — same two functions, the extractor run once per event. See [Verdict composition](#verdict-composition).

**Event path.** If there is no assistant message in the transcript, this is the session's first inference call: emit nothing (see the tail gap in [Improvement points](#improvement-points)); the verdict still runs. Otherwise build invocation N exactly the way `bedrock/` and `vertex/` build theirs: `message_to_normalized_invocation(AnthropicRequestBody(messages=everything before A), AnthropicMessage(role="assistant", content=A's blocks, stop_reason="tool_use" or "end_turn"))`, where a run of consecutive assistant messages is merged into one response by concatenating their content, then `finalize()` and `build_event_from_normalized()`. The shared helpers attribute `used_tools` and `accessed_files` to the round the model **consumed** — `U_prev`, the newest user-role run inside `input` — which is the same rule every forwarder follows. Each invocation is therefore reported one round late, and the session's last response is never reported at all; that is the cost of consistency and is stated in the README rather than worked around.

Field mapping:

| Envelope field | Source |
| --- | --- |
| `request_id` | content-addressed (below) |
| `identity_details` | `{"kind": "anthropic", "user_id": actor.id}` → `AnthropicIdentityDetails`; the adapter's `ResolveAIInvocationIdentity` returns `""` on a miss, so a brand-new seat builds the sub-graph without an identity edge and resolves on the next invocation. A null `actor.id` drops the event: the server rejects an Anthropic identity with no identifier as a permanent error. `kind` is a client-side discriminator only — the server's `oneOf` lists `aws` and `gcp`, push routes skip the request validator, and Go ignores the unknown field — so the `ng-evangelion` schema sync is batched with the other wire extensions rather than blocking this. |
| `conversation_id` | `session_id` passthrough when non-null |
| `timestamp` | the frame's `webhook-timestamp` header, Anthropic-attested, as ISO 8601. For the emit-previous record this is frame N+1's arrival, the nearest attested instant after invocation N ran; the frame carries no per-turn time. |
| `model` | `AIModel(id=model, provider="anthropic", raw_model_id=model)` built directly, as `vertex/event_envelope.py` does. `shared.model_catalog` is Bedrock-only and not used. A null `model` becomes `id="unknown"`, since `AIModel.id` is required and dropping the event would lose the identity and file record. |
| `input` / `output` | the full transcript before A / A, hashed by `build_event_from_normalized`, text subject to `SLASHID_INCLUDE_RAW_CONTENT` |
| `available_tools` / `available_tool_servers` | synthesized from the distinct `tool_use.tool_name` values in the transcript, through `build_tools_declared`, since the frame carries no definitions. Without them `_used_tools` cannot map a result to a tool id and emits nothing. |
| `used_tools` | shared `_used_tools`: `U_prev`'s `tool_result` blocks joined to their `tool_use`, with `is_error` |
| `accessed_files` | `attachment_files(transcript before A)` seeded, then `finalize` adds the `Read`-style tool results: both from `U_prev`, deduped |
| `stop_reason` | `tool_use` when `A` ends in a tool call, `end_turn` otherwise. On a denial record: `guardrail_intervened`, which only exists under enforcement — see [Denials emit immediately](#denials-emit-immediately) |
| `tokens` | zero — the frame carries no usage |
| `user_agent` | `source.application` (`claude-code`, `claude-ai`, `cowork`). The frame has no client user agent; the surface name is the nearest equivalent and is what a server-side HumanDriven rule can key on. |
| `parsed_as` | `"anthropic-inference-hook"` on this path; a reader-emitted event uses `"anthropic-compliance"` so a consumer can tell an untruncated event from a 10 KB-capped one |

A denial record differs in six rows, per [Denials emit immediately](#denials-emit-immediately): `request_id` is the frame's own, `input` includes `U_new`, `output` is absent, `stop_reason` is `guardrail_intervened`, and `used_tools` / `accessed_files` come from `U_new`. A frame of unknown top-level `type` emits nothing and is logged at WARNING with the value, which is how the response-side event is detected the day it ships.

### Denials emit immediately

Emit-previous alone would lose every denial. Deny frame N and inference never runs, so no assistant turn is produced and frame N+1 never arrives — the invocation is reconstructable from nothing, and the single most valuable record we could write (a prevented exfiltration) is the one we would never write.

So a denial that is **enforced** emits at denial time, from frame N alone: `input` is the full transcript including `U_new` (what the model would have seen), `output` is genuinely absent, `accessed_files` are the fresh round's, and `request_id` is the frame's own `request_id` (== `webhook-id`) rather than a content address — correct here for the same reason it is wrong for an invocation, since a denial *is* a delivery-level event and two blocked attempts are two incidents, not one re-revealed turn — there is no assistant `tool_use.id` to anchor on, and Anthropic's `req_…` ids sit in a namespace disjoint from both the `toolu_`-anchored and the `hook:`-prefixed content addresses, so the schemes cannot collide. A denied frame N still reveals `A`, so the emit-previous record for invocation N−1 is emitted as well: two events, on disjoint `request_id` namespaces, and the audit record loses nothing. The denial record is built through the same shared builder and then post-processed on the wire model — `output=None` and the `stop_reason` below — because `NormalizedInvocationOutput` has no absent state: an empty output still dumps `{"stop_reason": "unknown"}`, which the builder would hash into a spurious `output`.

**`stop_reason` on that record is decided, on measurement rather than on paper.** The obvious value is `guardrail_intervened`, but a deny is not always a block, and both halves of that are now observed rather than quoted:

- **Under shadow mode a denied request proceeds.** Our deny returned, inference ran anyway, the assistant answered, and the next frame carried the denied prompt with its reply like any ordinary round. `guardrail_intervened` would have been a straightforward lie.
- **Under Block the request a denied request is stopped.** The user saw the blocked-by-policy message, no assistant turn was produced, and the session wedged. `guardrail_intervened` is exactly right.
- **The frame cannot tell the two apart.** 200 frames across both modes are byte-identical in structure; there is no `is_shadow` and nothing to infer one from. A rollout percentage below 100 adds a third case the frame equally cannot reveal.

So the rule stands as designed, now for a demonstrated reason: **a denial record is emitted only when `SLASHID_ENFORCE` is true**, stamped `guardrail_intervened`, and that flag is an operator assertion the receiver cannot verify at the moment it answers. When the forwarder is observe-only its deny is not honoured by construction, inference runs, and frame N+1 reports invocation N through the normal emit-previous path; emitting a denial record too would record the same turn twice under different `request_id`s, exactly the double-count the content-addressing section rejects. The would-be denial is logged instead.

**What the measurement did add is a frame-derived check on that assertion.** Because an honoured deny produces no assistant turn, the *following* frame proves the outcome: two consecutive user-role runs with no assistant turn between them means the deny was honoured, while an assistant turn after the denied content means it was not. The receiver therefore never has to guess for longer than one turn. It cannot use that evidence at deny time, since the evidence does not exist yet, which leaves a real design option open: emit the denial record one frame late, the way invocations already are, and stamp `stop_reason` from observation instead of from the flag. The cost is losing every denial whose session the user simply abandons — and a wedged session is one a user is unusually likely to abandon, which is why v0 keeps stamping at deny time. Verification item 5 remains the cheaper route: if the activity feed distinguishes a shadow deny from an enforced one, the `reference_id` join settles the outcome with no flag and no delay.

### Two sources, three deployments

**Supersedes the inline-only design, as of the 2026-09-20 live run.** Two facts the events need — whether a denial was honoured, and what the final turn of a session said — exist only after the verdict, on a different surface. So the collector is **two independent sources over one event schema**, each sufficient alone.

| | **Inference hooks** (push) | **Compliance surfaces** (pull) |
| --- | --- | --- |
| Inline enforcement | **yes**, the only surface that can block | no |
| Latency | inline verdict; event one round later | minutes |
| Tool-result fidelity | **untruncated** | capped at 10,000 bytes |
| Final turn of a session | never | **yes** |
| Attachment bytes and real filenames | no | **yes** (claude.ai chats) |
| Client user agent | no, only `source.application` | only on denial activities |
| Model per turn | yes | yes on transcripts, **absent on denial activities** |
| Denial outcome | operator assertion | **authoritative** |
| Setup cost | Owner configures a hook; we host a public endpoint | a key; **nothing to deploy** |

"Compliance surfaces" means the Activity Feed, local-session transcripts and claude.ai chat messages with file content — not the much thinner **Data and privacy → Export audit logs** CSV.

#### Three facts measured before designing on them

Each underpinned a claim that would otherwise have been a guess.

1. **The actor identifier is the same on both sources.** A local session's envelope carries `user.id` `user_01AZE6…`, byte-identical to the frame's `actor.id`. So a compliance-emitted event builds the same `AnthropicIdentityDetails` as a hook-emitted one, and the same human does not fork into two graph identities depending on which source spoke. Without this, compliance-only could not emit at all, since an Anthropic identity with no identifier is a permanent server reject.
2. **`tool_use.id` is identical across sources.** The same `toolu_0193uWE72wFzQrTnb8L1f4AS` appears in the frame and in the compliance transcript. The content address therefore converges across sources **whenever the run contains a tool call**.
3. **A denial activity carries no model.** Its observed fields are `request_id`, `conversation_id`, `reference_id`, `surface`, `organization_id`, `organization_uuid` and an `actor` with `user_id` and a real client user agent. No model, so an event built from an activity alone has `AIModel.id` `"unknown"`.

#### Capabilities follow the credentials

There is no mode switch to set. **Each source turns itself on when its credential is present**, which makes the deployment self-describing and impossible to configure inconsistently:

| Configured | Capability |
| --- | --- |
| `SLASHID_HOOK_SIGNING_SECRET` | the hook: inline verdicts, and eventing from frames |
| `SLASHID_COMPLIANCE_KEY` | the pull readers: periodic log and transcript reads |
| both | both, joined |
| neither | startup fails; there is nothing for the component to do |

**Hook only** needs a push token and a signing secret, nothing else, and is the only configuration that enforces. It uses the same pending-and-deadline machinery as the joined mode, which is what rescues the final round: the record is completed by the next frame in the ordinary case, and flushed input-only when no next frame ever arrives.

**That flush is worth more than it looks, because it is the only record of the session's last file access.** `accessed_files` are attributed to the round the model consumed, so a `Read` in the final round belongs to the final invocation — the one emit-previous can never report. Without the deadline flush, a user who reads a sensitive file and then closes the session produces **no audit record of that read at all**, which is precisely the event this product exists to capture. The flush records it, with the input and the file digests, missing only the reply.

**Compliance only** runs the periodic pulls and emits from them. No endpoint is hosted, no certificate is needed, nothing is configured in claude.ai. It cannot enforce, and its tool blocks are capped at 10 KB, but it sees every turn including the last.

**Both joins them**, and the join is the reason to run both.

#### The join, and why expiry emits rather than deletes

The processor's dedup is terminal, so a turn cannot be emitted twice and enriched afterwards: the second event is discarded, not merged. Enrichment therefore has to happen **before** the single push. So the receiver never pushes directly. **In every configuration** it writes a **pending invocation** — an `AIInvocationObservedV1` built as far as the frame allows, with `output` outstanding — and something later completes and pushes it. That is one code path for all three capabilities, differing only in who can complete a record and how long it is worth waiting.

Everything in that record is already derived for the verdict, so nothing extra is computed and no prompt is stored beyond what `include_raw_content` already permits:

| From the frame | Awaiting a reader |
| --- | --- |
| `identity_details`, `model`, `timestamp`, `conversation_id` | `output` |
| `input` — untruncated hashes, and text only under `include_raw_content` | `stop_reason` |
| `accessed_files` for tool results, untruncated | attachment digests, which need bytes |
| `used_tools`, `available_tools`, `available_tool_servers` | |

**A pending record is completed by whichever source gets there first, and expiry pushes it regardless.** Three things can complete it, and the distinction matters:

1. **The next frame**, which reveals the output of the call the record is waiting on. This is emit-previous, unchanged, and it is the common case: it supplies `output`, `stop_reason` and the round's `used_tools`, untruncated and within seconds.
2. **A reader**, which supplies the same fields from the transcript and additionally the one thing no frame carries — attachment byte digests — and which is the *only* completion available for the final turn of a session.
3. **Expiry**, after `SLASHID_JOIN_WAIT_SECONDS`, which pushes the record as it stands.

So the record is not held waiting for compliance while a frame could finish it; it is held only until it is *complete enough*, and pushed on a deadline either way.

**One predicate decides when to push, in every configuration: a record is pushed once nothing an *enabled* capability could still supply is outstanding, or the deadline expires.** Two fields are ever outstanding, and which sources can fill them is what the capabilities decide:

| Outstanding | Filled by |
| --- | --- |
| `output`, `stop_reason`, `used_tools` | the next frame, always; or a reader, when compliance is enabled |
| attachment byte digests | a reader only, and only when the round contains an attachment |

That is the whole difference between the paths. With the hook alone, nothing but the next frame can ever add anything, so a record is settled the moment that frame arrives and pushes immediately — the latency and content of the original emit-previous design. With compliance also enabled, a round carrying an attachment stays outstanding until a reader hashes it, and a round carrying none is settled by the next frame exactly as before. **The wait is not a property of the mode but of whether anything is actually still coming.**

The measured shape of traffic makes that cheap: **356 Claude Code frames contained zero attachment blocks; 13 of 14 claude.ai frames contained one.** Claude Code puts file contents through `Read`, which the frame already hashes untruncated, so the overwhelming majority of rounds never wait even with both capabilities on. `SLASHID_JOIN_WAIT_SECONDS` only ever delays attachment-bearing rounds and final turns.

A frame-completed record is also **better hashed than a reader-completed one**: its `input` digests and its `accessed_files` entries for tool results come from an untruncated transcript, where compliance would have capped each tool block at 10 KB. The one thing no frame can supply is an attachment's bytes, since it carries only extracted text, or for an image nothing hashable at all.

**Exactly one push per record, whichever completer gets there first.** The full sequence for an attachment-bearing round with both capabilities on, the only case that exercises every path:

1. **Frame N** carries the attachment. The receiver writes pending record `P`: identity, model, timestamp, untruncated `input` hashes, `accessed_files` with the attachment entry holding an extracted-text digest or none. No push.
2. **Frame N+1** reveals the response. `P` gains `output`, `stop_reason` and `used_tools`, untruncated. Still outstanding: the attachment digest.
3. **Then exactly one of:** a reader fills the digest and pushes; or the deadline passes and `P` is pushed as it stands.

These are alternatives, never both. The dedup would discard a second push anyway, since both carry the same content-addressed `request_id`, so a pushed record is **retired with a short-lived tombstone rather than deleted** — otherwise a reader arriving after a deadline flush finds no record, treats the turn as one the receiver never saw, and emits a duplicate that exists only to be thrown away.

Neither ordering is guaranteed. A reader can arrive before frame N+1 and supply `output` as well as the digest, pushing once. A final turn never gets a frame N+1 at all, so only step 3 applies, and that flush is the input-only record.

**Expiry emits rather than deletes**, which is what keeps coverage at or above hook-only. An earlier draft deleted unmatched records and would have lost anything with no compliance counterpart. Two classes genuinely have none, and one I previously listed does not:

- **Zero-data-retention organizations**, where compliance captures nothing at all. That is a property of the whole deployment, not a race, and every record there flushes.
- **Sub-conversations.** Measured: the `web_search` sub-request does not appear in its parent session's transcript at all, while Haiku status summaries do appear as ordinary turns. They are not reliably distinguishable in a frame either — the only signals are a single-message transcript and a model differing from the session's, neither dependable — so their records simply flush.
- **Not** "sessions past retention", which the earlier draft claimed. Active sessions are persisted immediately: a probe session's transcript was readable seconds after it ran. Retention only bites for a deliberately short custom window, never for a record that lives an hour.

Turns unsampled under a partial rollout are the opposite of a problem here: they produce no frame and therefore no pending record, and compliance sees them anyway, so enabling both *widens* coverage past what the hook alone can reach.

**The flush of an uncompleted record is input-only** — identity, model, untruncated `input` and `accessed_files`, no `output`. That happens only for a genuine final turn with no transcript, and it is strictly better than hook-only, which loses that turn entirely. Recording what the user sent, even without the reply, is the point.

#### Provenance stays whole within each field

A joined event must not describe one byte string with another's digest. So enrichment is **all-or-nothing per field**, never a blend:

- `input` and its hashes come wholly from the frame, untruncated.
- `output` and `stop_reason` come wholly from the reader's transcript; `stop_reason` is **inferred** from block shape there, exactly as the hook path infers it, because no surface supplies it.
- `accessed_files` is per entry: a tool-result entry keeps the frame's untruncated digest, an attachment entry takes the reader's, and each carries a `provenance` saying which kind of file it is — see [`AIAccessedFile.provenance`](#aiaccessedfileprovenance).

`parsed_as` names the provenance of the event as a whole: `anthropic-inference-hook`, `anthropic-compliance`, or `anthropic-joined`.

#### Where convergence is guaranteed, and where it is not

Fact 2 gives convergence for any run containing a tool call, which is what lets a reader recognise the run a pending record was waiting for. For a **tool-free** run the fallback is a digest of session, ordinal and text, and every input is source-dependent: the frame omits turns whose blocks are all excluded, the transcript prepends a synthetic system message, one `session_id` carries many sub-conversations, and compliance truncates. A tool-free run therefore joins only by ordinal, which is weak, so **it flushes unenriched rather than risking a wrong join** — no worse than hook-only, which is the floor this design keeps.

Several pending records can await one run, since 21% of invocations are revealed by more than one delivery. The newest is the real input, because the transcript only grows, so **latest wins** and the superseded records flush unenriched and are dropped by dedup.

#### Reader A — denials, from the Activity Feed

Checkpointed on the activity cursor, polling `inference_hooks_request_denied`, filtering out its own `compliance_api_accessed` noise. **The correctness win**: an activity exists only when the block actually happened, measured both ways, so shadow-mode denials stop producing phantom block records and `SLASHID_ENFORCE` leaves the correctness path.

It emits from the activity alone: identity from `actor.user_id`, `conversation_id`, `surface` as `user_agent`, `request_id` from the activity's own `request_id`. **`model` is absent**, so it is filled from the conversation's transcript when Reader B has one and is `"unknown"` otherwise — stated plainly because a mode that made `"unknown"` the norm would collapse many models onto one graph node and disable model-allowlist detections.

#### Reader B — responses, from the Compliance API

**`provenance` is the anchor, not the ordinal.** A transcript interleaves three kinds of message, and the counts from one real session are the argument: of 500 messages, 246 were `client_asserted`, 3 were `synthetic_marker`, and the rest carried no provenance. Client-asserted messages are history the client re-sent, not turns produced by that call, and **only a newly-produced assistant turn carries a `model`** — 2 of 248 assistant messages in that window. So Reader B emits one invocation per assistant message that is newly produced, and skips replayed history and markers. That is a far stronger anchor than counting runs, and it sidesteps the ordinal collisions entirely.

The corollary is that **`model` is usually absent** on the messages Reader B does not emit for, and present on the ones it does — convenient, but it must inherit from the pending record or the session when a produced turn still lacks one, rather than writing `"unknown"` by reflex.

Polls local sessions and chats by `updated_at` with a lagging bound. Per newly-produced assistant turn it emits an event: `request_id` from the content address, `input` and `output` from the transcript, `model` from the message, `stop_reason` **inferred from block shape exactly as the hook path infers it** — no surface anywhere supplies it. Skips the synthetic first message. Honours the truncation flags by recording that the event is capped, via `parsed_as`.

A `clls_` session identifier decodes to `{"v":1,"o":<org uuid>,"p":<account uuid>,"s":<session uuid>}` where `s` is the frame's `session_id`, so frames and transcripts correlate exactly. Use the documented listing and decode `s` to match, rather than constructing the identifier: construction works but depends on an explicitly versioned encoding and on an account UUID no frame carries.

#### Attachment enrichment

Measured on the live tenant, and better than the documentation implies:

| Upload | Stored bytes | Matchable against a graph `FileHash`? |
| --- | --- | --- |
| `maria.txt`, 27 B | identical 27 B; digest equals the frame's | yes |
| `guiaSADT.pdf`, 59 KB | **a real PDF** (`%PDF-1.4…`) | **yes — the frame never could** |
| a JPEG | **a real JPEG**, 72,878 B against the frame's declared 70,657 | no, a processed copy |

**Neither the id nor the md5 is in the frame.** A frame's `attachment` block carries exactly `type`, `file_name`, `media_type`, `size_bytes` and `text` — verified against the capture — so the hook side has no file identifier and nothing to hash but extracted text. Both come from the *compliance* side: a chat message's `files[]` gives `id`, `filename`, `mime_type`, `size_bytes` and **`md5`**, and `…/files/{id}/content` streams the bytes. That is the whole reason attachment enrichment needs a reader.

It also means the reader does not try to pair a frame's attachment block with a `files[]` entry, which would be unreliable: `file_name` is null on the frame for images and PDFs, the `<uploaded_files>` order does not match the block order, and `size_bytes` disagrees whenever the stored copy was processed. **Instead it replaces**: for a message that has `files[]`, the frame-derived attachment entries are dropped and rebuilt from the listing, which is strictly better on every field — a real filename, a real digest, the stored size.

**The replace must not touch entries derived from tool results, and nothing on the wire distinguishes them.** Both kinds land in the same `accessed_files` list with the same shape, and we deliberately ship no provenance field, so a reader handed only that list cannot tell a replaced-from-`files[]` entry from a `Read` result it must preserve — and a naive "drop the attachment-looking ones" would silently delete the untruncated tool digests that are the better half of the record. The pending record is ours, not the wire event, so it **keeps the two groups in separate fields** and merges them only at push time. The reader replaces one field and never sees the other.

**The same file attached twice is two accesses, and that is the intended answer.** Within one round, `finalize`'s dedupe by name and hash collapses a repeat into one entry. Across rounds, each consuming invocation reports it separately, because two uploads are two events — the per-round attribution above is what produces that, and nothing should collapse them afterwards. Measured: uploading `image30.png` twice in one conversation produced **two different `claude_file_…` ids with an identical `md5` and size**, one on each user message. So the id is never an entry's identity and the digest is, which is what makes two accesses of one file recognisable as the same file without collapsing them into one event.

**An attachment is reported once, on the invocation that first consumed it, not on every turn thereafter** — the same consumption rule as everywhere else, and both sources already behave that way. On the compliance side `files[]` hangs off the single user message that carried the upload: in the measured conversation, three files on message 0 and none on any later message. On the hook side the transcript keeps re-sending the `attachment` block forever — it sat at index 0 of a three-message frame whose fresh round began at index 2 — so scoping to the round after the last assistant message is what excludes it. Without that scoping every subsequent turn in a conversation would re-report the same file and inflate the graph's edge counts by the length of the conversation. Measured: the `md5` in that listing equals the md5 of the downloaded bytes for all three test files, so **the cheap digest is already in a response Reader B fetches anyway**. (`HEAD` on the content endpoint is a 404; a per-file metadata endpoint exists and returns the same fields plus back-references, but is redundant given the listing.)

Two modes, differing in cost, in privacy, and in which digests they can produce:

| `SLASHID_ATTACHMENT_HASHING` | Requests | Bytes through the collector | Digests |
| --- | --- | --- | --- |
| `md5` *(default)* | **none extra** | **none** | `md5`, when the listing carries one |
| `full` | one per attachment | the whole file | `md5`, `sha1`, `sha256` |

**`md5` is the default because it is free in both senses.** It adds no request, and the file's bytes never transit the collector, which matters most in the SlashID-hosted deployment where those bytes are someone else's data. It is not a token tier either: a hash is only sensitive relative to a tenant's own tagged resources, and Salesforce-sourced resources carry md5 alone, so it genuinely matches them. Where the listing has no md5, the entry simply carries none.

**`full` is the opt-in for sha1 and sha256 matching** — what OneDrive, SharePoint and Drive resources need — and should be presented as what it is: the collector downloads customer files.

**Both modes hash what claude.ai stored, which is not always what the user uploaded.** The measured image came back 2 KB larger than the upload, a processed copy; some documents are stored as extracted text instead of bytes. We hash whatever is there and accept that such a digest will not match the original. That is a README line, not a wire field: a digest that does not match simply does not match, and inventing a provenance flag to say so would add a schema change for no decision anyone makes differently.

#### `AIAccessedFile.provenance`

One new field on the wire model, and it is what makes the rest of this section implementable. Without it the two kinds of entry are indistinguishable in a flat list, so a reader cannot replace one group without risking the other, and a consumer cannot tell an input from an output.

| `provenance` | What it is | Digest is over |
| --- | --- | --- |
| `tool_result` | a file the model read through a tool — Claude Code's `Read` and its siblings | the returned text, `cat -n` stripped, untruncated from a frame |
| `attachment` | a file the user uploaded into the conversation | extracted text from a frame; the stored bytes when a reader supplied it |
| `generated` | a file the model produced through tool use | the stored bytes, `md5` from the listing |

**`generated` is a deliberate stretch of the field's name**, and worth taking. A compliance chat message carries `generated_files` — files Claude produced, each with its own `md5` — and such a file was not *accessed*, it was created. But for a data-loss control what leaves a conversation matters as much as what entered it, and there is no other field where a produced file belongs. `provenance` is precisely what keeps the two readable apart once they share a list, which is why adding the field is the thing that makes including them safe. `artifacts` are left out for now: they are versioned documents rather than files, with their own id shape and no digest in the listing.

The field also carries the replace rule: **a reader replaces only `attachment` entries**, rebuilding them from `files[]`, and never touches `tool_result` ones, where the frame is the better source.

Per the standing convention that the server ignores unknown fields, this ships client-side first and the `ng-evangelion` schema sync batches with the other wire extensions.

#### One connection, or the dedup does not hold

Both sources must push under the **same** `{org, connection}`, because the dedup key is `ai_invocation_dedup:{org}:{conn}:{request_id}`. A separate *read* credential is fine and expected; a separate *push* connection would silently disable cross-source dedup. And because `read:compliance_user_data` reads every linked organization while the hook and its `tenant_id` binding are per-organization, **the readers must filter to the bound organization UUID** rather than emitting whatever the key can see.

#### Costs and limits

Event latency is minutes wherever a reader emits. Reader B needs `read:compliance_user_data`, a far larger credential than a push token and a real consideration for a customer-hosted collector. `stop_reason` is inferred in every mode and `tokens` stays **zero**, because no surface carries either — confirmed by scanning every captured response from the sessions, transcripts, chats, activities and configuration endpoints for a token, usage or cost field and finding none. The comparison table in Anthropic's own session documentation says the same and points at the Enterprise Analytics API instead, which reports per-user **daily** totals rather than anything per invocation.

That is a fidelity gap against the sibling forwarders rather than a missing feature of the product: `bedrock/` and `vertex/` read real per-invocation counts out of their logs, while the `anthropic` NHI adapter already pulls the daily Analytics figures separately. So the tokens exist in SlashID, just never on one of these events, and a consumer comparing sources must not read a zero here as "no tokens were spent". And a reader-emitted event is capped at 10 KB per tool block, which `parsed_as` declares.

### Sticky denials

Observed under enforcement on 2026-09-20, and a property of the protocol rather than of this receiver: **content that triggers a denial is never removed from the transcript, so it keeps triggering the denial.**

The mechanism is the interaction of three rules that are individually correct. The transcript is append-only and cumulative. The verdict scans the fresh round, which is everything after the last assistant message. A denial prevents inference, so no assistant message is appended. The offending block therefore remains inside the fresh round of every subsequent frame, and every subsequent turn in that session is denied, including turns whose own content is innocuous.

Three consequences to state plainly rather than discover:

- **For the user**, the session is unrecoverable. Anthropic's guidance is that `deny_reason` should tell the person what to change, but there is nothing they can change: the content is already in a history they cannot edit. The reason text should therefore tell them to start a new conversation, not to rephrase.
- **For the audit record**, a single act of touching sensitive content produces a denial event per subsequent turn, all with distinct frame `request_id`s. They are not duplicates and dedup will not collapse them, because each is a real blocked delivery. A detection counting denials will therefore over-count one incident as many unless it groups them itself: **same `conversation_id` plus the same `accessed_files` digests is one incident**, and nothing upstream will do that grouping.
- **It is also the post-denial signature.** A frame showing two consecutive user-role runs with no assistant turn between them is a call made after a denial, which answers verification item 7 affirmatively for the enforced case. Anthropic's warning that a turn whose blocks are all excluded is omitted entirely means the signature is sufficient but not necessary.

**"Forever" has two escape hatches, neither of them reassuring.** A rollout percentage below 100 means some turns are never inspected; such a turn proceeds, produces an assistant message, and the offending block falls out of the fresh round — so the session recovers precisely by letting the content reach the model. A tripped circuit breaker under **Allow the request** does the same for every turn at once. Both unwedge the session by abandoning the control, so neither is a fix, and at 100% rollout with a healthy server the wedge is permanent.

Scanning only the newest message rather than the fresh round would unstick the session, and is wrong: it would let the model consume denied content as soon as the user sent one more message. The stickiness is the control working. What needs to change is the message, not the mechanism.

### Content hashing fidelity

One normalization is load-bearing before a hash can match the graph's `FileHash` values, and one deliberately is not applied:

- **Strip `cat -n` numbering.** Claude Code's `Read` returns line-numbered text; hashing it raw can never match a stored file digest. `shared.content_utils.strip_cat_n` exists for exactly this and `extract_tool_result_files` already applies it.
- **No other normalization.** The shared extractor hashes the stripped text as-is, and the preflight hashes must equal the `accessed_files` hashes on the event for the same file, so the forwarder does not normalize line endings either. A CRLF file whose graph digest was taken over the original bytes matches only if `Read` preserved the CRLF. Recorded as a limitation, not patched on one path.

Even then, hashing *the frame* only works for **plain-text** files: a PDF or Office document reaches the transcript as extracted text, whose digest never equals the hash of the original bytes. That limits the hook, not the product — a compliance reader downloads the stored bytes, and a PDF measurably came back as a real PDF, so those files are matchable through that path. Within a frame the available signals remain `file_name` and `size_bytes`.

### `request_id` is content-addressed

**The obvious alternative — key the event on `webhook-id` — is wrong, and by a wide margin.** `webhook-id` is unique per *delivery*; the event's `request_id` identifies an *invocation*, and the two are not one-to-one. Measured over the 200 captured frames: 178 distinct invocations, of which **38 were revealed by two different `webhook-id`s**. Keying on the delivery would have emitted 77 events for those 38 invocations, double-counting Neo4j edge aggregates and appending duplicate BigQuery rows on 21% of traffic.

The reason is structural, not exotic. An invocation's output is the transcript's trailing assistant run, and that run stays trailing until the model produces a new one. Any frame arriving in between reveals the same invocation again: Claude Code's deferred-tool loading appends a "Tool loaded." user message and delivers a second frame, a user who types again before the model answers does the same, and a [sticky denial](#sticky-denials) does it indefinitely because no assistant turn is ever produced. Content addressing collapses all of them onto one key; the terminal dedup then drops the repeats silently.

Statelessness removes the receiver-side bookkeeping that would otherwise prevent emitting invocation N twice. Content addressing replaces it:

- Anchor on `A`'s first `tool_use.id` (`toolu_01…`) when present — model-minted and globally unique.
- Otherwise `"hook:" + hex(sha256(session_id or "", assistant-run ordinal, text digest))[:32]`. The fixed prefix keeps the fallback disjoint from `toolu_` ids and from Anthropic's `req_…` frame ids by construction, not by luck. A null `session_id` (documented as best-effort for Claude Code) leaves the ordinal and digest to disambiguate. The ordinal is computable from the frame alone (the transcript is append-only, so `A`'s position is stable across frames) and disambiguates two identical replies in one session, which a bare text digest would collide.

Every re-reveal of the same turn — Anthropic's connection-failure retry, a conversation fork that re-exposes an earlier turn, a redelivery — then converges on the same `request_id`, and the processor's dedup (`ai_invocation_dedup:{org}:{conn}:{request_id}`, terminal, 5-minute claim / 72-hour completion) drops the duplicate silently. Idempotency lives in the key rather than in remembered state, which is strictly more robust.

This is also why a half-event-then-completion design was rejected: that dedup is terminal. A second event on the same `request_id` returns `ResultDuplicate` and is discarded (and its done-sentinel TTL is *refreshed*, so late completions never land); a second event on a different `request_id` double-counts Neo4j edge aggregates and appends a second BigQuery row. The join has to happen before the push, and with a cumulative frame it can happen without state.

## Observed wire shapes

Captured on 2026-09-20 from the SlashID tenant with a capture-only receiver (`anthropic/deploy/dev-deploy.sh`, `SLASHID_CAPTURE_BUCKET`), driven by `claude-work` and claude.ai. Sanitized copies are the fixtures under `anthropic/tests/fixtures/`.

- **Transport.** `User-Agent: anthropic-dlp/1`, `Accept-Encoding: identity`, HTTP/1.1 at the Cloud Run frontend; bodies 1–5 KB for short Claude Code sessions.
- **Ids.** `webhook-id` == `request_id`; `msg_01…` from Claude Code and `chatcompl_01…` from claude.ai, so the frame-id namespace is not one prefix. `session_id` is the Claude Code session id, and on claude.ai the conversation UUID (matches `conversation_created` in the admin audit log). `tenant_id` is one UUID across both surfaces.
- **Claude Code first user message** carries two injected `<system-reminder>` text blocks (user email, git attribution) before the prompt text. They hash into `input` like any other text and must be scrubbed from fixtures.
- **`Read` results** are `N\tline` with no left padding and a trailing `N\t` for the final newline; `strip_cat_n` recovers the exact file bytes. Error results carry `is_error: true` and the error message as `content`.
- **Parallel tool use** is one assistant message with several `tool_use` blocks and one user message with all the results, in order.
- **Subagents** (`Agent` tool) arrive as separate transcripts with the parent's `session_id`, and their frames interleave with the parent's. The content-addressed `request_id` still separates them; the fallback digest includes text, so identical ordinals do not collide in practice.
- **A haiku-model frame** arrives in the same Claude Code session alongside the opus ones; it is a governed request and gets its own events.
- **Consecutive user messages happen.** Claude Code's deferred-tool loading produces a `tool_result` message followed by a separate user message ("Tool loaded.", plus any message the user sent mid-turn), so a round can be two user messages; `after_last_assistant` returns both. Messages the user types in quick succession are appended as **text blocks to one user message** (five blocks observed), not as new messages. No run of consecutive assistant messages has been observed; the merge stays as a guard.
- **Remote Control** (`/remote-control`, then messages from claude.ai into a local Claude Code session) keeps the same `session_id` and the same transcript; the claude.ai-side `session_01…` id never appears in the frame. The slash command and its local output are recorded as text blocks in the transcript.
- **Tool and MCP visibility.** No frame carries tool definitions, so available tools and configured MCP servers are invisible until used. No other Anthropic surface fills the gap: the Compliance API's session transcripts state that *"tool definitions and MCP server configuration are not part of the transcript"* and cap tool inputs and results at 10 KB by default; the Admin API exposes members, workspaces, invites, keys and usage reports, and nothing connector-shaped; and the Enterprise Analytics API's connector endpoint reports org-level daily **adoption** under normalized names (`atlassian`), not the tools available to an invocation. Synthesizing from observed names is therefore the ceiling on **every Anthropic surface**, not a limitation of hooks alone. It is a real fidelity gap against the sibling forwarders, which read the request body: `vertex/` populates `available_tools` from `tools[].functionDeclarations[]` and `bedrock/` from `toolConfig`, both carrying each tool's description and JSON Schema, and both listing tools that were declared but never called. An `AIInvocationObservedV1` from this receiver therefore has strictly poorer `available_tools` than one from the other two, and consumers that compare across sources need to know that absence here means unobservable, not unused. A used MCP tool arrives as `tool_use.tool_name = "mcp__<server>__<tool>"` (observed: `mcp__demo__echo`), which `parse_tool_name` splits into server and tool, so `available_tools` / `available_tool_servers` synthesized from observed names do carry MCP servers once any of their tools is called. Claude Code's deferred-tool loading shows up as a `ToolSearch` call whose result is the placeholder `[non-text content]`; definitions never appear.
- **Server-side tools are captured, flattened.** A Claude Code web search produced two governed frames in one session: the harness's own `WebSearch` client tool, and a separate Haiku sub-request whose assistant turn holds a `tool_use` named `web_search` with its `tool_result`. Anthropic normalizes its server tools into the ordinary `tool_use` / `tool_result` shape, so they are observed, but **nothing in the frame distinguishes server-executed from client-executed**. The shared normalizer stamps `tool_executor="client"` unconditionally, so a `web_search` call is mislabelled; `vertex/` by contrast marks its `code_execution` blocks `server`. Correcting it would need a name allowlist, which is guesswork against an open set, so v0 accepts the inaccuracy and records it here.
- **claude.ai's extended research runs entirely outside the hook.** Asked for a live weather forecast, claude.ai called `launch_extended_search_task` with a research brief and got back only `{"task_id": "wf-…"}`. The task then ran for about two and a half minutes and produced **zero frames** — not one inference call of the research agent was governed — before the parent conversation resumed with the finished report. The hook therefore sees the brief the model wrote, an opaque task id, and the final artifact; it never sees a single page the agent fetched or a single search result it read. Anthropic's scope note that *"ancillary requests ... aren't sent to your endpoint"* evidently covers this, so it is by design rather than a fault, but it is the largest content blind spot found: on claude.ai, anything an agentic research task pulls from the web reaches the model without inspection. `artifacts` is the consolation — its `tool_use.input` carries the whole artifact body, so generated documents are inspectable.
- **A server tool's results are not inspectable.** That `web_search` `tool_result` came back as repeated `[non-text content]` placeholders, and a deferred-tool `ToolSearch` result is the same. Placeholder text is all there is to hash, so content a server tool returns into the model is outside the DLP check entirely. The file-exfiltration control covers client tool results, which is where `Read` lives; it does not cover what Anthropic's own tools fetch.
- **Claude Code status summaries** ("Current state: working …") arrive as separate haiku-model frames in the same session; they are governed requests and produce their own events.
- **claude.ai attachments.** A text file has `file_name`, `media_type`, `text` and null `size_bytes`; an image has `media_type`, `size_bytes` and null `file_name` and `text`; a PDF has `media_type`, extracted `text` with CRLF line endings, and null `file_name` and `size_bytes`. No resource id anywhere and `metadata` is empty. claude.ai injects an `<uploaded_files>` text block listing `/mnt/user-data/uploads/<name>` paths, in an order that does not match the attachment blocks, so names for the nameless blocks are recovered by pairing on extension and media type. `attachment_files` uses that name when the block's own is null.
- **Single-turn session** produced exactly one frame: the tail gap is real.
- **Shadow-mode deny** was not blocked, and the next frame showed the denied prompt and its reply in the transcript like any other round (verification item 6, shadow case).
- **Our own Compliance API reads are themselves audited.** Every query lands in the feed as a `compliance_api_accessed` activity carrying the `api_key_id`, source IP, user agent, request URL and status. Useful for detecting a leaked key, and a reason to expect the feed to carry collector noise alongside tenant activity: a polling reconciliation pass would log one activity per request it makes.
- **Nothing in a frame reveals the enforcement mode.** Diffed 99 frames captured under **Shadow mode** against 101 captured under **Block the request**: identical header names, identical top-level body keys, `metadata` empty (`{}`) in all 200, `source` carrying only `application`, `actor` only the three documented fields. There is no `is_shadow`, and no field from which the mode can be inferred. This is what the design assumed, now measured, and it is why `stop_reason` on a denial cannot be derived from the frame and why denial records are gated on our own `SLASHID_ENFORCE` instead. **A better home for that flag exists:** an administrator can configure up to 16 static custom request headers in the same dialog that sets the mode, so asserting the mode as a header keeps the two settings side by side in one console rather than split between claude.ai and a Cloud Run environment variable. Nothing enforces agreement either way, but co-located settings drift less. Worth adopting if a customer ever enforces for real.
- **An enforced deny is sticky, and that is the single most operationally important observation.** Under **Block the request**, a Claude Code session read a file whose contents matched the deny rule. Frame N+1 carried that `tool_result` and was denied, so inference never ran and no assistant turn was produced. The user's next message arrived as frame N+2 whose transcript still held the offending `tool_result`, followed by the new user text with **no assistant message between them** — and it was denied too. Because the verdict scans the fresh round and the fresh round is everything after the last assistant message, denied content stays in the fresh round for as long as the denial keeps preventing an assistant turn. The session is wedged permanently and the only recovery is to start a new one. See [Sticky denials](#sticky-denials).
- **Compliance API enrichment.** The admin audit export records `file_uploaded` with a UUID and the original filename; the Compliance API's chat-messages endpoint carries `files[]` with `filename`, `size_bytes` and `md5` per user message, and file content is downloadable by id. Stored content is a processed copy for images and extracted text for some documents, so those digests match neither the frame's text hash nor the original bytes. This is the deferred reconciliation pass, not the inline path.

## Verdict composition

Two checks, run concurrently, each optional, ANDed.

**Policy.** `POST {SLASHID_POLICY_URL}` with the **raw request bytes** and the three `webhook-*` headers copied verbatim. The Go receiver re-verifies the signature under its own copy of the secret, checks `frame.request_id == webhook-id` and the exact tenant binding, resolves the actor and evaluates the saved policy. Re-serializing the body would break its signature check, so the forwarder never does, and the forward is sent uncompressed: the receiver answers 415 to any `Content-Encoding` other than identity. It answers HTTP 200 with `{"action", "deny_reason"?, "reference_id"?}` — the Anthropic verdict shape — and turns its own evaluation errors into an explicit deny, so a 200 deny from it is authoritative and is never softened by our fail mode. Body cap on that side is 10 MiB; a frame over it is a transport failure here.

**Preflight.** `POST {SLASHID_ENDPOINT}/ip/nhi/ai/preflight` with the connection push token, per PR #7733:

```json
{ "identity_details": {"kind": "anthropic", "user_id": "user_01…"},
  "model": {"id": "claude-sonnet-4-5"},
  "accessed_files": [ {"name": "tests/auth_test.py", "content_hashes": {"sha256": "…", "sha1": "…", "md5": "…"}} ] }
```

The response carries one `AIPreflightVerdict {allowed, verified, message?}` per check plus `overall`. The forwarder reads `overall` only:

| `overall` | Forwarder |
| --- | --- |
| `allowed: false, verified: true` | deny; `message` becomes `deny_reason` |
| `allowed: true, verified: true` | allow |
| `allowed: true, verified: false` | apply `SLASHID_VERDICT_FAIL_MODE` |

`model` is sent when the frame carries one and omitted when null, since a check runs only when its input is present; preflight's model verdict is always `allowed: true` today and model policy belongs to the policy receiver, so preflight is called only for content. More than 100 hashable files in one round, or a request body over preflight's 1 MiB cap, is treated as an unverified check — fail mode applies, and the overflow is logged — rather than silently truncated. When the fresh round carries nothing hashable, preflight is not called: a request that asks nothing answers `verified: false`, which would spuriously engage the fail mode.

**Composition.** Any deny denies; the first denying check supplies `deny_reason`, to which the forwarder appends one fixed sentence telling the person to start a new conversation, because [sticky denials](#sticky-denials) mean the current one cannot recover and Anthropic's own guidance to say what to change is otherwise unfollowable. The result is truncated to 500 characters on a character boundary (Anthropic truncates there anyway; the policy receiver's reasons are short and preflight's are already capped). `reference_id` is always the forwarder's own, `hex(sha256(webhook-id))[:32]` — the same recipe the Go receiver uses, stable across Anthropic's retry, within `[A-Za-z0-9._:/-]`, and carrying no content.

**It is, however, redundant as a join key, which the live capture made obvious.** The `inference_hooks_request_denied` activity already carries `request_id` verbatim, and that equals the `webhook-id`, which is exactly what a denial event's own `request_id` is set to. The activity therefore joins to our denial event directly, with no hash in the middle, and `reference_id` buys nothing a join needs. Since the field is ours to choose, is capped at 50 characters and never shown to the end user, the better use is to spend it on something the feed cannot otherwise tell us: **which check denied**. A value shaped like `slashid:preflight:<short digest>` or `slashid:policy:<short digest>` would let the feed alone separate a sensitive-content block from a graph-policy block, which is the first question anyone reviewing a denial asks. Deferred rather than changed here, because it diverges from the Go receiver's recipe and that alignment should be a deliberate decision rather than a side effect. A transport failure (timeout, non-200, connection error) on either check applies `SLASHID_VERDICT_FAIL_MODE` to that check. A disabled check — `SLASHID_POLICY_URL` unset, or `SLASHID_PREFLIGHT_ENABLED=false` — is skipped and does not count as unverified; that is how the receiver deploys before the Go route and PR #7733 are live. An enabled preflight against an endpoint that answers 404 is a transport failure, not a skip.

**`config-test` frames and frames of unknown top-level `type`** skip both checks and answer allow. Probes carry no user content, and the Go receiver denies both a probe whose actor it cannot resolve and any frame whose `type` is not `prompt`; forwarded, an unknown event type would come back as an authoritative deny, the opposite of the protocol's forward-compatibility rule. A valid verdict of either kind is what resets the circuit breaker, so allow is safe there. The Go receiver also denies `actor.type != "user"`; a future actor kind is therefore blocked by the chain under enforcement even though the forwarder itself tolerates it, which is a policy-receiver decision and not a forwarder bug.

`SLASHID_HOOK_ALLOW_UNSIGNED=true` together with a configured `SLASHID_POLICY_URL` cannot work: the Go receiver answers 401 to an unsigned forward, which the forwarder classifies as a transport failure and runs through the fail mode on every frame. Config validation rejects the combination.

**`SLASHID_ENFORCE=false`** runs every check, logs the composed verdict at INFO with its reasons, and answers allow. That keeps shadow-mode metrics real while the receiver is observe-only.

### Failure isolation — two rules

**1. An eventing failure must never become a verdict failure.** A non-200 response is a *webhook failure*, which hands control to the organization's fail-open/fail-closed setting. If a sink timeout propagated, a customer configured to "block the request" would have their engineers blocked because our BigQuery path hiccupped. So: respond first, push afterwards in a background task, log on failure, and never let the push's outcome reach the response. `SLASHID_PUSH_BUDGET_MS` bounds that task to protect the instance, not the verdict. Sustained webhook failures also trip Anthropic's circuit breaker, which disables enforcement entirely — an eventing outage must not be able to cause that.

**2. The verdict's own failure mode is a separate, explicit knob.** If a check fails or times out we still have to answer. Default **allow**, log loudly, and emit a metric; refusing to answer is self-inflicted downtime, and the customer already has Anthropic-side failure handling if they want strictness. Two settings that are easy to confuse — the doc and the README must name both and say which covers what.

**Budget.** Anthropic's verdict timeout is 1–10,000 ms, 5,000 ms by default, covering connection, TLS, request and response. Anthropic retries **once**, after 100 ms, and **only** when the connection attempt fails; once we have responded the exchange is never retried. One shot per frame. The Go evaluator caps itself at 2.5 s and preflight's graph deadline is 750 ms; both run in parallel under `SLASHID_VERDICT_BUDGET_MS`, so the verdict path costs one round trip to the slower check. The event push runs in a tracked `asyncio` task spawned after the verdict is decided (not a Starlette `BackgroundTasks` entry, which the ASGI test transport would wait for); the service is deployed with CPU always allocated so that task actually runs. `SLASHID_PUSH_BUDGET_MS` still bounds it so a hung sink cannot pin an instance: the shared sink retries with tenacity under `max_retries` and `request_timeout_seconds`, so the bound is an `asyncio.wait_for` around `push_invocations`, not a sink setting. At the default 2000 ms the wait expires inside the sink's first attempt, so its retry settings are inert here; the README says so.

## Companion: `POST /ip/nhi/ai/preflight`

Designed and implemented in `ng-evangelion` [PR #7733](https://github.com/slashid/ng-evangelion/pull/7733), spec `2026-09-18-ai-preflight-endpoint-design.md`. Summary of what the forwarder relies on:

- **Auth:** the connection's push bearer token — the same credential as `POST /nhi/events/ai-invocations`. One credential, one service, org/connection scoping for free. Only preflight lives under the `/ip/` prefix; the events path keeps its historical unprefixed route.
- **Request:** reuses `AIInvocationObservedV1` field types. `accessed_files[]` are `AIAccessedFile` with a multi-algorithm `content_hashes` map (`md5`, `sha1`, `sha256` only; any other key is a `400`). At most 100 entries, 1 MiB body. `identity_details` is free-form there and reserved.
- **Response:** `overall` plus per-check `AIPreflightVerdict`s. `verified: false` is the explicit "apply your own fail mode" signal; a lookup that cannot complete is never an error status.
- **Disclosure:** denial messages echo only the request's own `name`, never the matched graph resource, so the endpoint is a membership oracle over the tenant's sensitive-tagged corpus and nothing more.
- **Multi-algorithm is required, not optional.** There is no cross-algorithm equivalence: Salesforce publishes only MD5, OneDrive/SharePoint often only SHA-1. The forwarder sends all three.

## Configuration

`Config(BaseConfig)` adds to the shared base:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SLASHID_HOOK_SIGNING_SECRET` | required unless `HOOK_ALLOW_UNSIGNED` | `whsec_…`; comma-separated accepts two during rotation |
| `SLASHID_HOOK_ALLOW_UNSIGNED` | `false` | escape hatch for an org that enabled hooks before secrets were required |
| `SLASHID_POLICY_URL` | unset | the Go receiver's `/ai-access/<id>` URL; unset skips the policy check |
| `SLASHID_PREFLIGHT_ENABLED` | `true` | call `{SLASHID_ENDPOINT}/ip/nhi/ai/preflight`; false skips the content check |
| `SLASHID_VERDICT_FAIL_MODE` | `allow` | `allow` or `deny` when a check fails or answers unverified |
| `SLASHID_VERDICT_BUDGET_MS` | `3500` | our internal budget for both checks, under Anthropic's configured timeout |
| `SLASHID_PUSH_BUDGET_MS` | `2000` | hard cap on the event push; exceeding it logs and drops |
| `SLASHID_ENFORCE` | `false` | ship observe-only; enforcement is opt-in |
| `SLASHID_MAX_BODY_BYTES` | `33554432` | request body cap; Cloud Run's HTTP/1 limit is 32 MiB |

Compliance settings, in the same `Config`. Their presence is what enables the readers:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SLASHID_COMPLIANCE_KEY` | unset | `sk-ant-api01-…` with `read:compliance_activities` and `read:compliance_user_data`. **Setting it enables the readers.** |
| `SLASHID_ORGANIZATION_UUID` | unset | the bound organization, equal to the frame's `tenant_id`. Required with the key: it can read every linked organization, so the readers filter to this one. |
| `SLASHID_POLL_LAG_SECONDS` | `120` | how far behind now the `updated_at.gte` bound sits; too small silently drops sessions |
| `SLASHID_MAX_SESSIONS_PER_TICK` | `200` | bounds a tick against the 600 rpm shared with the sync adapter |
| `SLASHID_ATTACHMENT_HASHING` | `md5` | `md5` or `full`; see [Attachment enrichment](#attachment-enrichment). `md5` costs no extra request and moves no file bytes; `full` downloads each attachment to compute sha1 and sha256. |
| `SLASHID_PENDING_ENABLED` | `true` | hold each invocation in the pending store until something completes it. `false` restores the stateless receiver of the original design and loses the final round of every session, its file reads included. |
| `SLASHID_JOIN_WAIT_SECONDS` | `3600` | how long a pending record waits for enrichment before being pushed as it stands. An hour is chosen over minutes because the reader's end-to-end lag is set by its poll interval, the 600 rpm shared with the sync adapter and its own downtime, not by the API's freshness. Longer means more enrichment and a bigger store; shorter means more input-only flushes. Only meaningful when both credentials are set. |

Two validation rules. **At least one credential must be present**, or startup fails, since the component would have nothing to do. And `SLASHID_HOOK_SIGNING_SECRET` is required only when the hook is in use, so a compliance-only deployment does not have to invent one — which the receiver's current validator would otherwise demand.

## Tests

Fixture-driven, matching the house pattern in `bedrock/tests` and `vertex/tests`:

- `test_signature.py` — valid, wrong secret, tampered body, skewed timestamp, unsigned, URL-safe-decoder regression (a secret containing `+`/`/` must still verify), dual-secret rotation, case-insensitive headers.
- `test_frame.py` — unknown block `type`, unknown `source.application`, unknown `actor.type`, unknown top-level `type`, absent `metadata`, null `session_id`/`tenant_id`/`model`, non-alternating turns; translation to `NormalizedMessage` skips unknown blocks.
- `test_policy.py` — raw bytes and the three headers are forwarded unchanged (assert on the captured request); allow, deny with reason, 200 deny on evaluation error is honoured; non-200 and timeout raise.
- `test_preflight.py` — request carries all three algorithms and the identity; hashes are derived from the full message list and, across the `frame_tool_result.json` / `frame_tool_result_extended.json` pair (N's `U_new` is N+1's `U_prev`), equal the event's `accessed_files` hashes for the same file, attachments included; `overall` rows map to allow / deny / unverified; not called when nothing is hashable; 404 raises.
- `test_verdict.py` — both allow; policy deny wins with its reason; preflight deny wins with `message`; transport failure honours `VERDICT_FAIL_MODE` per check; unverified honours it; `enforce=False` allows while still evaluating; `config-test` and unknown top-level `type` allow without calling either check; `reference_id` charset and length; `deny_reason` ≤ 500 chars.
- `test_event_envelope.py` — first frame emits nothing; tool-result frame emits invocation N with `input` = transcript before A and `output` = A; `used_tools` and `accessed_files` come from `U_prev`, not `U_new`; `cat -n` stripped before hashing; parallel `tool_use` blocks; content-addressed `request_id` stable across two frames revealing the same turn; under `enforce` a denial emits immediately, keyed on the frame's own `request_id`, with `output` absent, `stop_reason=guardrail_intervened`, and the emit-previous record for N−1 alongside it; without `enforce` no denial record is emitted; an attachment with an unregistered `media_type` still translates; null `actor.id` drops.
- `test_main.py` — a sink failure still returns 200 with the correct verdict (**rule 1**, the regression that matters most); a slow sink does not delay the response; oversized body; verdict budget exceeded; unsigned gets 401; with both credentials present a frame writes a pending record instead of pushing, and with only the hook it pushes directly.

For the compliance half, against recorded fixtures of the real API responses:

- `test_client.py` — `clls_` decode yields the frame's `session_id`; the synthetic first message is skipped; truncation flags are surfaced; an own `compliance_api_accessed` activity is filtered out; a session outside `SLASHID_ORGANIZATION_UUID` is skipped.
- `test_denials.py` — an activity emits a denial event with identity, conversation and surface; `model` is `"unknown"` when no transcript supplies one; the cursor advances only past stored events; a sticky denial emits one event per activity.
- `test_responses.py` — `client_asserted` and `synthetic_marker` messages emit nothing; a newly-produced assistant turn emits one invocation; an assistant run's content address is **byte-identical to the one the hook path computes for the same run** (the convergence test, on the measured `toolu_` anchor); `stop_reason` is inferred, never read; the tail turn of a session emits.
- `test_attachments.py` — `md5` takes the digest from the message listing and makes no request; a listing with no md5 yields an entry with no digest; `full` downloads and computes all three, and its md5 equals the listing's; the plain-text case equals the frame's digest.

And for the join, which is where the risk concentrates:

- `test_pending.py` — a record is written rather than pushed in every configuration, and pushed directly only when `SLASHID_PENDING_ENABLED=false`; the final round of a session flushes input-only and **carries the fresh round's `accessed_files`**, which is the read that would otherwise never be recorded; the next frame completes `output` on an open record rather than pushing a second event; the newest record for a run wins and superseded ones flush; **a record older than `SLASHID_JOIN_WAIT_SECONDS` is pushed as it stands, not deleted** (the coverage regression that matters most); an uncompleted flush carries `input` with no `output`; a tool-free run flushes rather than joining on ordinal alone.
- `test_config.py` — capabilities derive from credentials: hook only, compliance only, both, and neither failing startup; a compliance-only config needs no signing secret.

## Deployment

Cloud Run service, one Terraform module attached to the release, mirroring `vertex/deploy/terraform` in shape:

- The release workflow builds the container image with `uv` and pushes it to GHCR as `ghcr.io/slashid/slashid-anthropic-forwarder:<version>`, alongside the GitHub Release.
- The module creates an Artifact Registry **remote repository** proxying `ghcr.io` (with optional upstream credentials, since the source repo is private), a `google_cloud_run_v2_service` pulling through it, `min_instance_count = 1` (a cold start inside a 5 s verdict budget risks a webhook failure, and enough of those trip the circuit breaker), CPU always allocated, public ingress, unauthenticated invoker, and Secret Manager entries for the push token and the signing secret. An `image` variable overrides the computed reference so a locally built image deploys during testing without cutting a release.
- **One service, two routes.** `POST /{path}` is the hook; `POST /tick` is the reader, fired by Cloud Scheduler with an OIDC token, concurrency 1 so checkpoint writes cannot race. Which route does anything is decided by the credentials, so the same image and the same module serve all three configurations.
- **Hook only** needs the pending store but no scheduler and no checkpoint store. `min_instance_count = 1`, because a cold start inside the verdict budget risks a webhook failure and enough of those trip the circuit breaker. The deadline flush needs something to fire it: the same `POST /tick` route, on a slow schedule, or a Firestore TTL-triggered function — either way it is far cheaper than the compliance poller.
- **The store costs the receiver its statelessness**, which was an explicit goal, and that is a real trade rather than a free win: two instances handling consecutive frames of one session now share state, where emit-previous needed no coordination at all. `SLASHID_PENDING_ENABLED=false` restores the stateless receiver and gives up the final round of every session, including its file reads. Default on, because losing the last read is the worse failure for a DLP product.
- **Compliance only** needs no public endpoint, no certificate and no minimum instance — a scheduler, a checkpoint store and two secrets. It is the cheapest deployment by a wide margin and the only one that needs nothing configured in claude.ai.
- **Both** adds the pending store. Firestore fits and is already in the stack; `vertex/` provisions a named database conditionally and this should mirror it, with a TTL policy as a backstop rather than as the flush mechanism, since flushing is the reader's job and must emit.
- Cloud Run caps HTTP/1 request bodies at 32 MiB, under the protocol's 64 MiB ceiling. Recorded as a known limitation; in practice bodies stay under 10 MB.

## Rollout

Anthropic provides staged rollout server-side, so we use it rather than building our own:

1. **Shadow mode** — verdicts observed on live traffic, nothing blocked. Validates the verdict path and calibrates false positives on real content.
2. **Rollout percentage** — a fraction of requests inspected.
3. **Role exclusions** — exempt chosen roles.
4. **Enforcement**, with the customer choosing fail-open or fail-closed.

Ship with `SLASHID_ENFORCE=false` so the receiver is observe-only even at full rollout until a customer opts in.

**Sampling has a consequence for the event path.** *"Each request rolls once for its whole conversation turn, so a single conversation can be partially inspected across turns."* We never receive the frames of uninspected turns, so frames are not consecutive. Emit-previous survives this — each frame we do receive carries the cumulative transcript, so the invocation it reconstructs is whole — but any invocation whose *successor* turn went unsampled is never emitted. Because `request_id` is content-addressed, an optional knob could walk the entire transcript on every frame and emit every reconstructable invocation, letting the pipeline's dedup collapse the repeats and restoring complete coverage at low sampling percentages. The cost is push volume growing with conversation length, so it is a knob rather than the default.

Denials are recorded as `inference_hooks_request_denied` activities carrying the `reference_id` we returned — the one cross-surface join Anthropic purpose-built, and what closes the enforce → audit loop. Whether a **shadow-mode** deny is recorded the same way is unverified and load-bearing (verification item 5). The feed also records *"requests that proceeded without inspection under your failure handling setting"*, which is a ready-made input for coverage-gap alerting rather than something we would have to infer.

## Improvement points

Tagged, not built in v0.

1. ~~**The final assistant turn of an *allowed* session is never captured.**~~ **Closed by Reader B**, which returned exactly such a turn from a compliance transcript. The paragraph below describes the hook in isolation and is kept because it still governs a hooks-only deployment. Originally: No subsequent frame exists to reveal it, so a multi-turn session loses its closing summary and an allowed single-turn interaction emits no event at all. Enforced denials are unaffected — they emit at denial time; an observe-only would-be denial on a final turn falls into the same gap. The README must say plainly: the verdict still ran, so this is a gap in the audit record rather than in inspection.
2. **Response-side hook event.** Anthropic states *"response-side enforcement is planned as a later event."* Logging unknown top-level `type` values while returning `allow` — which the protocol already requires — means we detect it the day it ships, and it closes (1) with no redesign.
3. ~~**Attachment enrichment from the Compliance API.**~~ **Built into Reader B**, and its central caveat turned out to be wrong: the measured PDF came back as original bytes, not extracted text, so this path *does* close the document gap for files claude.ai stores intact. Kept for the mechanics. The frame carries no resource id, but claude.ai uploads are addressable after the fact: `GET /v1/compliance/apps/chats/{id}/messages` returns a `files[]` array per user message with `id`, `filename`, `mime_type`, `size_bytes` and **`md5`**, and `…/files/{file_id}/content` streams the stored bytes with a `Content-MD5` header. Joining on the conversation id, which the frame's `session_id` supplies for claude.ai, would give authoritative filenames for the attachments that arrive nameless today (we currently guess them from the `<uploaded_files>` text block) and an `md5` to carry on `AIAccessedFile.content_hashes`.

   **It does not fix the PDF and Office gap, which is the reason one would want it.** The documentation is explicit that the stored content is not always the uploaded file: images are served as a processed copy, and Word, PowerPoint and some PDF files are stored as *the text claude.ai extracted from them*, with `size_bytes` and `md5` describing that stored content. So for exactly the formats whose digests never match a graph `FileHash`, the Compliance API returns another digest of the extracted text rather than of the original bytes. It adds a correlation key and a filename, not matching power.

   Three further costs to weigh before building it: it is asynchronous by nature and can never feed a verdict, which is budget-bound; it needs `read:compliance_user_data`, which is not a broader grant than the hook itself already carries — the receiver is handed every governed transcript untruncated and in real time — but it is a *differently shaped* one, since a pull key reaches backwards into history predating the hook and into files and projects the hook never sees, so it is worth provisioning as its own key rather than folding into this component; and it covers claude.ai only, since Claude Code attachments are `Read` tool results that are already hashed correctly from the frame.

4. ~~**Compliance API reconciliation pass.**~~ **Became Reader B.** Verified end to end on 2026-09-20 against the live tenant; retained here for the constraints, which still bind.

   **The join key exists, hiding in plain sight.** A local session's `clls_` identifier is not opaque: it is URL-safe base64 of `{"v":1,"o":<organization uuid>,"p":<account uuid>,"s":<session uuid>}`, where `o` is the frame's `tenant_id` and **`s` is the frame's `session_id`**. So a frame maps to its transcript exactly, with no time-window or content heuristics. Two ways to use it, and the safer one is not the clever one: constructing the identifier works (it was constructed and fetched successfully) but depends on an undocumented, explicitly versioned encoding, and also needs `p`, an account UUID no frame carries; **listing sessions and decoding each `s` to match** needs neither, uses only the documented listing endpoint, and degrades to a miss rather than a wrong answer if the encoding changes.

   **The final assistant turn is there.** Fetching the transcript of a session whose last turn the hook never saw returned that turn, with its text and its serving model. The tail gap — [improvement point 1](#improvement-points) — is therefore recoverable rather than permanent, which is the single strongest reason to build this pass.

   **Three things it still does not recover**, checked against the responses rather than assumed:

   - **No `stop_reason`.** A message carries `role`, `content`, `model`, `provenance` and truncation flags, and no stop reason. The feed is metadata, the Analytics API is daily aggregates. The receiver's inference from block shape remains the only source there will ever be.
   - **No tokens.** Explicitly absent from both session endpoints; only the Enterprise Analytics API has usage, as per-user daily totals.
   - **Truncation.** Tool inputs and each tool-result text are capped at 10,000 bytes by default, about 1 MiB on request, so a large `Read` result is not byte-identical to what the frame carried and its digest will differ.

   One shape detail for the implementer: the transcript's first message is a synthetic marker standing in for the system prompt (`provenance.type: synthetic`, text `[system prompt content not shown]`), so it must be skipped rather than treated as user content.

   The operational constraints already noted still apply: 600 rpm per *parent* organization shared with the sync adapter's existing compliance reads, an N+1 fetch shape, `updated_at.gte` polling whose bound must lag the previous run or sessions are silently dropped, and now one more — every request the pass makes is itself logged to the feed as `compliance_api_accessed`, so a poller adds its own noise to the tenant's audit record.

   **Joining a denial is separate and already solved.** The `inference_hooks_request_denied` activity carries `request_id` (the `webhook-id`), `conversation_id` (the `session_id`) and our `reference_id`, so it joins to a denial event three ways. There is no allowed-request activity to join to at all: an allowed, inspected request produces none, and our own event is the only record of it.

5. **Reimplementation in the Go servers**, per the delivery follow-up.
6. **gate plugin variant**, reusing the `anonymizer` plugin's Presidio and Trufflehog scans for a richer verdict than hash matching alone, and `monitoring_mode` to pair with shadow mode.
7. **OpenAI Codex coverage** via the Compliance Logs Platform — post-hoc only; there is no OpenAI inline hook.

## Verification list

Needs a live Claude Enterprise tenant with hooks enabled; none of it is answerable from the documentation.

1. **Answered (2026-09-20): `tenant_id` *is* the Compliance API's `organization_uuid`.** `GET /v1/compliance/organizations` returns one organization whose `uuid` is byte-identical to the `tenant_id` on every captured frame, and activities for it carry that same value in `organization_uuid`. So a frame binds to a SlashID connection on the organization UUID, and the Go receiver's `tenant_id` binding takes that value. Two corollaries worth writing down: an organization has **two** identifiers, a tagged `org_…` and the UUID, and activities carry both, so a consumer must not assume one shape; and the id the claude.ai settings page displays is **neither** of them — it matched nothing in the API — so it must never be copied into a binding.

2. ~~Do Claude Code subagents carry the parent's `session_id`?~~ **Yes** (observed 2026-09-20), as separate transcripts.
3. **Decided (2026-09-20): yes, set `HumanDriven`.** A seat-authenticated `actor.type: user` on an interactive surface is genuine evidence of a human, and leaving it unset flags every seat user `is_ai_agent`. The existing `Reason` values are `{mfa, console_user_agent, sso_role, saml_head}`, so this needs a new one — the receiver already carries the evidence as `user_agent` = `source.application`, and the resolver is server-side, so it lands as an `ng-evangelion` change batched with the schema sync, not in this component.
4. **Answered (2026-09-20), and one number is larger than the design assumed.** Over 200 captured frames: median 262 KB, p90 1.4 MB, **max 1.47 MB** for a 334-message Claude Code working session; claude.ai frames stayed under 12 KB. Receipt-to-response was 39–42 ms for the largest frames with capture on and no checks wired, so the receiver's own cost is negligible and `VERDICT_BUDGET_MS` will be dominated entirely by the two outbound calls. Cloud Run's 32 MiB cap is comfortable. Two consequences:

   - **The policy forward re-uploads the whole frame.** Chaining sends those same 1.47 MB to the Go receiver on every turn of a long session, doubling the bytes on the wire and putting an upload, not a computation, inside the verdict budget. The Go side's own cap is 10 MiB, so a session roughly seven times longer than the one measured would start failing that check and taking the fail mode.
   - **Capture is not free at this size.** A megabyte-scale object per turn accumulates quickly; the bucket wants a lifecycle rule before anyone leaves capture on beyond protocol study.
5. **Answered (2026-09-20): the Activity Feed is the authoritative record of what was actually blocked, and the enforcement state is reconstructible from it.** Two identical denials were produced, one with Mode on **Block the request** and one on **Shadow mode**, and the feed distinguishes them:

   | Our verdict | Anthropic honoured it | `inference_hooks_request_denied` |
   | --- | --- | --- |
   | deny, Block the request | yes, request stopped | **recorded** |
   | deny, Shadow mode | no, request proceeded | **absent** |

   The enforced record carries our `reference_id` verbatim, so the join is exact; the shadow deny produced nothing at all. A denial activity therefore means the request was genuinely blocked, and its absence means it was not.

   **And `inference_hooks_config_updated` records the entire configuration on every change**, timestamped and attributed: `enabled`, `shadow_mode`, `fail_mode` (`fail_closed`), `rollout_percentage`, `prompt_verdict_timeout_ms`, `final_verdict_timeout_ms`, `enforcement_mode`, `webhook_url`, `extra_header_names` and the deny-message settings. Replaying those activities reconstructs exactly which mode was in force at any instant, which is the fact the frame refuses to reveal.

   **What this changes in the design, and what it cannot.** `SLASHID_ENFORCE` stops being a correctness requirement and becomes a best-effort inline label: the receiver still has to answer before any of this exists, so it still stamps `guardrail_intervened` from the flag. But the label is now *auditable* — a `reference_id` with no matching activity is a denial that did not take effect, and a mislabelled event is detectable rather than silently wrong. It cannot be repaired in place, because the processor's dedup is terminal and an emitted event is immutable, so the correction belongs in a SlashID-side detection that joins the two, not in a later update to the event.

   One forward-looking detail fell out of the same record: `enforcement_mode` reads `prompt_only` and there is a separate `final_verdict_timeout_ms` beside the prompt one. Anthropic's configuration already models the response-side hook that [improvement point 2](#improvement-points) waits for, which is the strongest signal yet that it is coming.

6. ~~Is an enforced denial's prompt retained in the conversation transcript?~~ **Yes** (observed 2026-09-20, Block the request). The denied `tool_result` remained, and the next frame showed two consecutive user-role runs with no assistant turn between them. See [Sticky denials](#sticky-denials).
7. ~~Can a call made after a denial be identified?~~ **Yes** for the enforced case: the two-consecutive-user-runs signature above. It is sufficient but not necessary, since a turn whose blocks are all excluded is omitted entirely, so a detection built on it under-reports rather than false-positives.
8. **Does the `user_01…` actor id match the identifier the `anthropic` adapter stores for org members**, so `ResolveAIInvocationIdentity` resolves on the first invocation rather than never?

## Revision notes

Changes from the first draft, after review on 2026-09-18:

- **Chains to the existing Go receiver** for graph policy instead of ignoring it; a Claude org has one hook URL.
- **Preflight replaces `sensitive-files/check`** (PR #7733 superseded #7728): request reuses `AIAccessedFile`, response is `{allowed, verified, message}` per check, `verified: false` engages the forwarder's fail mode.
- **Event reconstruction follows the shared convention**: `input` is the full transcript before A; `used_tools` and `accessed_files` are attributed to the round the model consumed, not the round it requested. The draft's "join A's `tool_use` to `U_new`'s `tool_result`" is dropped for consistency with `bedrock/` and `vertex/`, at the cost of also losing the last round's reads in the tail gap.
- **`shared/normalize/anthropic` is reused for the frame's messages** via an alias on the tool-use name and a new attachment block, instead of a parallel frame schema.
- **FastAPI on uvicorn in a container** on Cloud Run, not a bare ASGI app or functions-framework; asyncio-native so the checks run concurrently and the push runs after the response.
- **`config-test` and unknown event types bypass both checks**, because the policy receiver denies unresolvable actors and non-`prompt` frames.
- **Spec review fixes:** the extractor takes the full message list; `AIModel` is built directly (the model catalog is Bedrock-only); `timestamp` is the attested `webhook-timestamp`; one `reference_id` rule; a denied frame also emits the N−1 record; no line-ending normalization; denial records only under `SLASHID_ENFORCE` (observe-only would double-record the turn); attachments enter `accessed_files` through one helper shared by the verdict and event paths.

### After live testing on 2026-09-20

The receiver was deployed to a test tenant in capture-only mode, driven with `claude-work` and claude.ai, then run once under **Block the request**. Eleven observations are folded into [Observed wire shapes](#observed-wire-shapes); these changed the design rather than merely confirming it:

- **[Sticky denials](#sticky-denials)** were discovered, not predicted. Denied content stays in the fresh round forever, so one denial wedges the session permanently. `deny_reason` now has to tell the person to start a new conversation, and a detection counting denials must expect many events per incident.
- **Server tools are visible but unlabelled**, and their results are `[non-text content]` placeholders; `tool_executor` is therefore wrong for them and the DLP check cannot see what they fetched.
- **claude.ai's extended research emits no frames at all**, which makes agentic sub-task traffic a stated non-goal rather than an assumed coverage.
- **Tool and MCP inventory is unavailable on every Anthropic surface**, so an "may not use that MCP server" policy is unenforceable inline. Now a non-goal.
- **Frames reach 1.47 MB**, so the policy forward re-uploads megabytes per turn and the Go receiver's 10 MiB cap becomes a real ceiling on session length.
- **`tenant_id` is not the org id claude.ai shows**, which would have broken the Go receiver's tenant binding on first configuration.

## References

- [Inference hooks overview](https://platform.claude.com/docs/en/manage-claude/inference-hooks)
- [Develop an Inference hooks integration](https://platform.claude.com/docs/en/manage-claude/inference-hooks-endpoint) — frame and verdict schemas, signature, operational semantics
- [Configure Inference hooks](https://platform.claude.com/docs/en/manage-claude/inference-hooks-configuration) — failure handling, circuit breaker, shadow mode
- [Compliance API](https://platform.claude.com/docs/en/manage-claude/compliance-api) and [session transcripts](https://platform.claude.com/docs/en/manage-claude/compliance-sessions) — the deferred pull path
- [Compliance API FAQ — data coverage](https://platform.claude.com/docs/en/manage-claude/compliance-faq) — the coverage boundaries quoted above
- [Standard Webhooks](https://www.standardwebhooks.com/)
- `ng-evangelion`: [PR #7733](https://github.com/slashid/ng-evangelion/pull/7733) and `docs/superpowers/specs/2026-09-18-ai-preflight-endpoint-design.md`; `docs/superpowers/specs/2026-09-13-ai-access-hooks-policy-design.md` and `backend/modules/detections/components/aiauthorization/README.md`; `docs/superpowers/specs/2026-07-02-index-file-hashes-design.md`, `docs/superpowers/specs/2026-07-02-ai-accessed-files-plumbing-design.md`
- This repo: `docs/superpowers/specs/2026-08-27-vertex-ai-forwarder-design.md`
