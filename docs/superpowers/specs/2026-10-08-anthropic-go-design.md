# Anthropic forwarder in Go: one core, two hosts

**Date:** 2026-10-08
**Status:** Draft for review. Written against this repo at `f9fe1e0` and `ng-evangelion` at `967b11467d`. Statements about either come from reading source; what could not be checked is in "Decisions needed".
**Target repos:** `slashid-ai-forwarders` (a new `go/` tree beside `anthropic/` and `shared/`) and `ng-evangelion` (an adapter, an endpoint, a scheduler job, tables, edge routes).
**Builds on:** `2026-09-21-anthropic-ai-invocations-design-v2.md` (the product design, unchanged), `2026-10-01-local-platform-design.md` and `2026-10-07-anthropic-local-design.md` (the platform seam this document generalises).

## Problem

The Anthropic forwarder is a Python service on Cloud Run. Its hook already calls `ng-evangelion`'s preflight over HTTP on the verdict's critical path, and its tick re-implements credentials, scheduling and deduplication that `ng-evangelion` already has for every other source. We want the same logic to run inside `ng-evangelion` (one deployment, in-process preflight and publish, credentials from the connection) **without** losing the stand-alone service, which remains the fast development loop and the deployment for anyone not running `ng-evangelion`.

## Goals

1. A Go implementation whose behaviour matches the Python one wherever behaviour is observable: `request_id`, addresses, content and round hashes, wire JSON, store documents.
2. One core, two hosts. The core knows nothing about Cloud Run, Firestore, Postgres, `ng-evangelion` or HTTP routing; a host supplies a small set of ports.
3. Stand-alone stays first-class: its own binary and image, its state behind the same ports, a test suite that runs in seconds without an `ng-evangelion` checkout.
4. The Cloud Run deployment is replaceable in place: same environment variables, same Terraform, same Firestore documents, same local SQLite files.
5. Integrated in `ng-evangelion` through `POST /ip/nhi/events/ai-invocations/preflight/anthropic` (Bearer push token; reachable at `…/anthropic/v2` during the transition, see "Gate and edge routing"), a per-connection tick job, and credentials from the connection's encrypted store.
6. Parity is measured, not assumed: a golden corpus produced by the Python implementation inside its production image, an offline differential replay, and an isolated live shadow.

## Non-goals

- Changing what the forwarder does. Addressing, ownership, the tail record, the soft join, denial confirmation and failure isolation are the v2 design, ported as is.
- Porting `bedrock/`, `vertex/` or `codex/`. The core is shaped so they can follow; nothing here depends on them.
- Replacing `ng-evangelion`'s Anthropic directory/usage adapter or its Activity Feed poller. They read the same Compliance API for other purposes and stay.
- Porting the Python `shared/` package wholesale. Only what the Anthropic path reaches.
- Reproducing Pydantic's lax coercion (`"5"` → `5`). Strictness is deliberate and recorded under "Accepted deviations".

## Decisions

| Question | Decision | Reason |
| --- | --- | --- |
| Where does the code live? | `go/` in this repo, as **two** modules: `go/forwarder` (lean) and `go/standalone` (heavy). `ng-evangelion` imports only `go/forwarder` | Module graph selection is not pruned away: importing a module that requires Firestore, gRPC and SQLite drivers puts those requirements into `ng-evangelion`'s `go.mod` and can raise versions it already pins. The lean module needs only the standard library, `golang.org/x/text` and the Standard Webhooks library, and its `go` directive must not exceed `ng-evangelion`'s (1.27.1). Tests that need a SQL driver live in `go/standalone`; the lean module holds only driver-free code, including the contract suite as an exported package that takes a store factory |
| Does the core import `ng-evangelion` types? | No. Hand-written wire types, checked against `ng-evangelion/spec/ai-schemas.yaml` by a contract test | Stand-alone must build without it; wire fields ship client-side before the spec (standing convention) |
| Parsing | Hand-written structs with small union helpers, `encoding/json` v1, no generators for vendor shapes | Prototype (2026-10-08): ogen rejects the schema (`complex anyOf`); oapi-codegen and go-jsonschema lose the smart-union and the `tool_name` alias; hand-written matched Pydantic 13/13. `jsonv2` is still `GOEXPERIMENT`-gated on Go 1.27.1 |
| Pending store | One contract, three adapters: Firestore (document-compatible with Python), SQL (SQLite and Postgres through one compare-and-set implementation with a configurable table layout), memory | Firestore enables in-place replacement; SQL covers the local platform and `ng-evangelion`'s Postgres; memory is for tests |
| Scope of a `Forwarder` | One per connection; stores are partitioned by an opaque `scope` | `ng-evangelion` is multi-tenant, stand-alone is one tenant, the code is the same |
| Tick trigger in `ng-evangelion` | A per-connection Cloud Scheduler job to an internal endpoint, **not** `SourceEventPoller` | `PollEvents` runs once a day after a full sync, keeps one timestamp, and buffers all events in memory. The reader needs minutes, three watermarks and a lease |
| Hook authentication in `ng-evangelion` | Bearer push token identifies the connection; Standard Webhooks HMAC is verified whenever the connection has signing secrets configured | Custom headers are available on Anthropic's side. HMAC proves the body came from Anthropic and not from a token holder |
| Event delivery in `ng-evangelion` | In process: build the `UnprocessedPushedEventV1` and `ipPublisher.Publish` | The payload `IngestAIInvocationEvent` produces, without the HTTP hop |
| Failure of work before the verdict | Recover and answer per the fail mode (a deliberate deviation from Python, which returns 500) | A non-200 is a webhook failure to Anthropic and feeds its circuit breaker |

## Architecture

```
                   ┌────────────────────────── go/forwarder ──────────────────────────┐
                   │                                                                   │
 Anthropic ─POST─► │  anthropic.Forwarder                                              │
 (hook frames)     │    HandleFrame(ctx, FrameRequest) FrameResponse                   │
                   │    Tick(ctx) (TickResult, error)                                  │
                   │      hook/  compliance/  pending/  address/  record/              │
                   │      core: wire canon textutil normalize rounds reads mediatype   │
                   │         │ needs, through ports                                    │
                   │   Store · Checkpoints · Lease · Sink · Preflighter · Compliance   │
                   │   · Clock · Observer · BlobSink                                   │
                   └──────────┬───────────────────────────────────────┬────────────────┘
                              │                                       │
               go/standalone (this repo)                    ng-evangelion adapter
               net/http, env config                         echo handler, connection credential
               Firestore | SQLite | memory                  Postgres tables, ipPublisher
               HTTP Sink + HTTP Preflighter                 in-process Preflight, scheduler job
```

### Ports

```go
type Deps struct {
    Store       pending.Store     // seven operations
    Checkpoints Checkpoints       // one watermark per feed
    Lease       Lease             // the tick's mutual exclusion
    Sink        Sink              // delivers finished events
    Preflight   Preflighter       // asks the organization's policy about the tail event
    Compliance  compliance.Source // the Compliance API (HTTP by default; a fake in tests)
    Clock       Clock
    Observer    Observer          // counters and logs; may be a no-op
    Capture     BlobSink          // optional raw-frame capture; nil in production
}

type Sink interface {
    // Push delivers events as given; an implementation that needs to stamp
    // fields (org, connection) does so on a copy. An error is retried by the
    // caller with the store's backoff, as Python does for every push failure,
    // unless it is a *PermanentError: the record is then retired after the
    // bounded attempts a validation failure gets. The stand-alone HTTP sink
    // never returns one (Python retries every push error); the ng-evangelion
    // sink does, for an event the downstream resolver will always reject.
    Push(ctx context.Context, events []wire.Event) error
}

type Preflighter interface {
    // DenyReasons judges the tail event within budget. An empty slice is a real
    // allow. An error means no verdict at all (transport, non-200, unparseable)
    // and is mapped through the fail mode. A server-side deny is never an error.
    DenyReasons(ctx context.Context, tail wire.Event, budget time.Duration) ([]string, error)
}

type Checkpoints interface{ Feed(name string) Checkpoint }
type Checkpoint interface {
    Load(ctx context.Context) (Mark, error) // zero Mark on a cold start
    Save(ctx context.Context, m Mark) error
}

type Lease interface {
    // Hold runs fn while holding the lease and releases it on every way out.
    // held=false (fn not run) means another tick holds it. The lease is renewed
    // while fn runs; if a renewal fails, fn's context is cancelled and Hold
    // returns the error, so a tick never runs on a lease it no longer holds.
    Hold(ctx context.Context, ttl time.Duration, fn func(context.Context) error) (held bool, err error)
}
```

These are the Python `Platform` seam (`checkpoint_store`, `tick_lease`, `blob_sink`) plus `Sink`, `Preflighter` and `Compliance`, which Python wires directly to `httpx`. `scheduler_auth` is **not** a port: it belongs to the stand-alone host's HTTP layer, and `ng-evangelion` guards its internal endpoint with the platform's own internal auth.

### The `Forwarder`

```go
func New(cfg Config, deps Deps) (*Forwarder, error) // applies Config's validators

type FrameRequest struct {
    Body    []byte      // raw bytes as received; the HMAC is over exactly these
    Headers http.Header // webhook-id, webhook-timestamp, webhook-signature
}
type FrameResponse struct {
    Status  int                    // 200, 401, 413
    Verdict *Verdict               // the body of a 200
    After   func(context.Context)  // work to run after responding; never nil on 200
}
func (f *Forwarder) HandleFrame(ctx context.Context, r FrameRequest) FrameResponse

type TickResult struct {
    Skipped  bool            // another tick holds the lease
    Flushed  int
    Counters map[string]int  // the reader counters, by the names Python returns
}
func (f *Forwarder) Tick(ctx context.Context) (TickResult, error)
```

`HandleFrame` returns the response **and** the work to do after it, and the host decides how to run that work (a tracked goroutine in stand-alone; the adapter's own drained group in `ng-evangelion`). `After` is returned on every 200, including the shortcuts that need no pending write, because Python queues raw-frame capture as a background task after a successful signature check, for unparseable and non-prompt frames too. Work after the verdict cannot alter a response that has already been sent; **work before it can still fail**, which is why "Failure policy" below exists.

`New` is cheap, so `ng-evangelion` keeps a small cache of `*Forwarder` per connection (see "Integration").

## Module layout and the Python mapping

```
go/
├── go.work                              local development only
├── forwarder/                           module github.com/slashid/slashid-ai-forwarders/go/forwarder
│   ├── core/
│   │   ├── wire/                        events.py + AIPreflightResponse + validate-at-push
│   │   ├── canon/                       CPython-exact JSON rendering and parsing
│   │   ├── textutil/                    content_utils.py, Python strip/splitlines/slicing semantics
│   │   ├── normalize/                   normalize/{turn,finalize,anthropic,normalized/*}.py
│   │   ├── rounds/                      rounds.py
│   │   ├── reads/                       reads.py, codex_output.py (shlex port)
│   │   ├── mediatype/                   media_types.py + the embedded mimetypes table
│   │   └── pushclient/                  sink.py: HTTP push (1 MB batches, retry) and preflight
│   ├── anthropic/
│   │   ├── config.go                    config.py + BaseConfig (struct and Validate; no env here)
│   │   ├── forwarder.go                 main.py: handle_frame, run_tick
│   │   ├── address/ record/ pending/    address.py record.py pending.py
│   │   ├── hook/                        hook/{frame,envelope,checks,verdict,preflight,signature,capture}.py
│   │   └── compliance/                  compliance/{client,schema,checkpoint,readers,responses,denials,attachments,softjoin}.py
│   └── store/
│       ├── contract/                    the suite every adapter runs
│       ├── memory/
│       └── sqlstore/                    database/sql only, no driver; layouts for SQLite and Postgres
└── standalone/                          module github.com/slashid/slashid-ai-forwarders/go/standalone
    ├── cmd/slashid-anthropic-forwarder/
    ├── firestore/                       pending store + checkpoints + lease, Python-compatible
    ├── sqlite/                          modernc.org/sqlite driver, checkpoints + lease, Python-compatible
    ├── blobsink/                        GCS and local-file BlobSinks for raw-frame capture
    └── httpserver/                      routes, scheduler-OIDC auth, shutdown
```

`sqlstore` uses `database/sql` without importing a driver, so `ng-evangelion` brings its own (`lib/pq`) and the lean module stays dependency-light. Everything in Python `shared/` that the Anthropic path does not reach (Bedrock/Converse/Gemini/Llama/DeepSeek normalizers, `model_catalog`, local file hashing) is not ported.

## Fidelity

This is where the port can fail silently, so it gets the most space. The terminal's dedup is `{org}:{connection}:{request_id}`, first-completed-wins, and every address in the pending store is a cross-source key. A Go service that computes a different `request_id` for the same invocation double-counts it.

### Parsing

| Pydantic | Go | Notes |
| --- | --- | --- |
| `_LenientModel` (`extra="ignore"`) | Plain struct; `encoding/json` ignores unknown fields | Compliance rows, vendor blocks, the frame |
| Smart-union with a catch-all (`AnthropicContentBlock`) | `Block` with one non-nil variant and an `UnmarshalJSON` that peeks `type`, tries the typed variant, **falls back to `Unknown`**; no string `type` is an error | A `tool_use` without `id`, or `text` with `text: 5`, degrades to `Unknown`. Prototype matched Pydantic 13/13 |
| `validation_alias=AliasChoices("name","tool_name")` | Both fields; normalize in `UnmarshalJSON` | Not recoverable from `model_json_schema()` |
| `Rows[T]` (drop unparseable rows, log) | `type Rows[T any] []T` whose `UnmarshalJSON` decodes element by element and counts what it drops | A bad row costs one row, never the tick |
| JSON `null` on a non-nullable field (`actor`, `messages`, `metadata`), `content` as a string | Parse error, as Pydantic. Go's decoder treats `null` as absent, so these fields use a small `NonNull[T]` wrapper | A frame that fails to parse is answered `allow` with no record |
| Frame-derived strings (text, tool names and ids, file names) | A lossless string type: WTF-8 bytes with a custom JSON string decoder, so a lone surrogate (`\ud83d`, which JS clients emit and JSON allows) survives | `encoding/json` would turn it into U+FFFD **while decoding the frame**, before `canon` runs, and the hashes would silently diverge from Python's |
| `JsonValue` (tool inputs/outputs) | A value tree that keeps big integers and distinguishes int from float; `NaN`, `Infinity`, `-Infinity` are accepted on parse (CPython does) | Never passed through as raw tokens: `1.00` renders as `1.0`, `1e2` as `100.0`, as CPython |
| `X \| None` with `exclude_none=True` | `*X` with `omitempty` for nullable fields; plain value with no `omitempty` for fields Python always emits (`tokens` zeros, `is_error: false`) | Set **per type and per location**: nulls inside a `JsonValue` are kept, top-level `None` is dropped, the hashed normalized messages always emit `tool_is_error: false`, and `rounds._Block` has its own projection |
| `NormalizedContent.tool_input` truthiness | `input if input else {}`: `None`, `0`, `false`, `""`, `[]` all become `{}` | Feeds the round hashes; ported literally |
| dict/set insertion order | Ordered structures where Python relies on it (`_tool_names`, `servers_by_id`, `used_tools`, dedupe sets) | Go maps are never iterated for output |

### Validation: at construction and at push

`_WireModel` is **decoded**, not only encoded, and its validators run in two places. They run at **construction**: Python builds `AIAccessedFile`, `AITool` and `AIToolServer` through pydantic constructors all through the pipeline, so the strip and the file-name reduction happen before hashing and dedupe. Two consequences are observable: `finalize` dedupes on the *reduced* name, so two reads of `__init__.py` from different directories collapse into one entry; and `_tool_id_index` keys on a stripped `AIToolServer.name` while `used_tools_of` looks up with the unstripped result of `parse_tool_name`, so a name with surrounding whitespace drops a used tool. And they run at **push**: `record.to_event` calls `AIInvocationObservedV1.model_validate(apply_file_digests(stored_document, file_digests) + parsed_as)`, where `parsed_as` is decided from `contributed` (`anthropic-joined` when more than one source contributed, else `anthropic-compliance` or `anthropic-inference-hook`). The Go wire constructors apply the validators, and `wire.Validate(document) (wire.Event, error)` re-applies them idempotently at push. Together they carry:

- `extra="forbid"` on every wire type, so an unexpected key fails the record (the retry/discard policy under "The tick" applies);
- `str_strip_whitespace=True`: every string field is stripped with **Python's** `str.strip` (which also strips `\x1c`–`\x1f`, `\x85`, NBSP and the other Unicode spaces; Go's `TrimSpace` differs);
- `AIAccessedFile.name` reduced to a file name by `PureWindowsPath(...).name`, including drive letters, UNC shares and `scheme://` prefixes;
- the `identity_details` discriminated union, the at-least-one-identifier rule for Anthropic identities, and the `Literal` enums.

### Byte-exact hazards

Each row has a dedicated differential test against CPython 3.14 in the production image.

| Hazard | Python behaviour | Go requirement |
| --- | --- | --- |
| Canonical JSON (`input`/`output`/round hashes, `_short_hash` tool and server ids, the 1 MiB record bound) | `json.dumps(sort_keys=True, separators=(",",":"))`, `ensure_ascii=True`: non-ASCII as `\uXXXX`, astral characters as surrogate pairs, keys sorted by code point, `/` not escaped | `core/canon`: its own encoder; never `encoding/json` (which escapes `<>&` and emits UTF-8). `record._sizeof` and the elision steps use the same encoder |
| Numbers | `float` repr (`1.0`, `1e+16`, `1e-05`); arbitrary-precision ints; `NaN` emitted bare | `canon` renders by CPython repr rules from a parsed value tree |
| Lone surrogates | A JS client's `\ud83d` is valid JSON: Python parses it and `json.dumps(ensure_ascii=True)` re-escapes it identically, so the hashes contain `\ud83d`; but `.encode()` raises | The lossless string type above carries it through decoding, and `canon` re-escapes it. The `.encode()` sites that raise are `address.py` (ids, `tool_use_id`, and `_prefix` of text, name and `file_name`), `envelope.py` (attachment text), `tool_results.py` and `anthropic/normalize.py`. Those reached from `unanswered_round` run **before** the verdict (Python: a 500, so Go's deviation 2), and those reached from the write-behind fail that record (the flush policy) |
| `truncate_middle` | `\b` is Unicode-aware; slicing by code point; the tail loop `break`s after the **first** boundary whether or not it is accepted | Hand-written scan over runes using Python's `\w` classes; the single-boundary quirk is ported and has a golden case |
| `strip_cat_n` and `.strip()` | `str.splitlines` splits on `\v \f \x1c-\x1e \x85    `; `strip` is Unicode-aware; `\s`/`\d` are Unicode | `textutil` ports `splitlines`, `strip`, `isspace`, `isdigit` by Python's tables |
| Regexes | `$` also matches before a trailing newline (`otel.py`); `(?!0$)` lookahead (`codex_output.py`) | RE2 has neither. Hand-written matchers for these two, with a case for the trailing-newline input |
| `tail_address` | NFC-normalize **then** truncate to 256 **code points**; length-prefixed fields | `golang.org/x/text/unicode/norm`, truncate by rune. Unicode table versions may differ from CPython 3.14's; the differential test shows whether it matters |
| String slicing on the wire | `deny_reason[:500]`, `base[:500-len(RECOVERY)]` slice by code point | Slice by rune; never mid-rune |
| `mimetypes.guess_type`, the `MimeType` registry | CPython's built-in table plus the host's `/etc/mime.types` if present | An embedded table generated **inside the production image** (whose `mime.types` presence decides the output) and pinned |
| `shlex.split` and the shell-meta check | POSIX `shlex` with comments off | Ported with the same quoting and escape rules |
| `signed_at_iso` | `datetime.fromtimestamp(ts, UTC).isoformat()` → `2026-10-08T10:00:00+00:00`; `isoformat()` drops the fraction when microseconds are zero | Explicit layout `2006-01-02T15:04:05+00:00`, not RFC 3339 |
| `datetime.fromisoformat` | Accepts many spellings; may return a **naive** value, and comparing naive with aware raises `TypeError` (`client._before`, `responses._walk`, `denials`) | A tolerant parser tested on every spelling in the compliance fixtures. Go's `time` has no naive/aware split, so the places where Python would raise are listed and given the same outcome (a caught error) |
| `hashlib` over `str.encode()` | UTF-8 | As Go strings; see lone surrogates |
| The Compliance → Anthropic round trip (`to_anthropic(_dump(...))`) | Lossy: `UnknownBlock` dumps only `type`; a `thinking` block becomes an empty thinking block | Reproduced, because addresses and hashes depend on it |
| `json.loads` limits | `1e400` becomes `inf` and re-renders as `Infinity`; integers over 4300 digits raise (the frame is answered `allow`); a `bytes` body is sniffed for a BOM and UTF-16/32 | The same behaviours, each with a corpus case |
| `PureWindowsPath(...).name` | Returns nothing for `.`, trailing separators, `C:` and repeated separators | Golden cases for each |
| The verdict body | Starlette JSON (`ensure_ascii=False`, compact); empty reasons omitted; fixed texts "Blocked by your organization's policy." and "Your organization's policy check is unavailable." | Reproduced exactly |
| Compliance timestamps | Query bounds use `since.isoformat()` (`+00:00`, no fraction when zero); events carry Anthropic's raw timestamp string untouched | Same formatting; strings are never re-rendered |
| Counters | `round()` is banker's rounding (`responses_max_first_seen_lag_s`) | `math.RoundToEven` |
| Unicode classes | `\w`, `\b`, `isspace`, `isdigit` and NFC all depend on the Unicode version | One pinned table version for the Go side, compared with CPython 3.14's by the differential tests |

### Accepted deviations

Recorded here and added to the shadow-run diff taxonomy, so they are expected, countable differences rather than surprises:

1. **No lax coercion.** A wire field Python accepts as `"5"` for an integer is a parse failure in Go. The golden corpus and the shadow run show whether any real traffic does this.
2. **Pre-verdict panics and exceptions are recovered** and answered per the fail mode (Python returns a 500; for example `{"text": null}` in a tool result raises in `_bytes_from_tool_output`).
3. **Tick bounds.** Python's lease is not renewed and a Cloud Run tick is bounded only by the scheduler's 540-second deadline; the local ticker has no bound. Go bounds every tick with a hard deadline, renews the lease while it runs, and cancels the tick if a renewal fails.
4. **Signature verification is strict.** Python's library decodes the body as UTF-8 and re-encodes it (invalid UTF-8 is a 500), signs `floor(float(timestamp))`, and decodes the secret leniently. Go verifies the raw bytes against the timestamp header and rejects malformed secrets at configuration time. Invalid signatures remain 401.
5. **No-secret mode in `ng-evangelion`.** With no signing secrets configured the Bearer token alone authenticates a frame; Python rejects every frame when no secret is configured.

## The pending store

The seven operations (`upsert`, `complete`, `claim`, `due`, `nearby`, `retire`, `seen`) and their contracts are unchanged from v2.

```go
type Store interface {
    Upsert(ctx context.Context, scope, address string, p Patch, expect []string, now time.Time) (Outcome, error)
    Complete(ctx context.Context, scope, address string, p Patch, clears []string, now time.Time) (Outcome, error)
    Claim(ctx context.Context, scope, address string, lease time.Duration, owner string, now time.Time) (*Record, error)
    Due(ctx context.Context, scope string, now time.Time, limit int) ([]Record, error)
    Nearby(ctx context.Context, scope, conversationID string, at time.Time, window time.Duration, limit int) ([]Record, error)
    Retire(ctx context.Context, scope, address string, how Retirement, now time.Time) error
    Seen(ctx context.Context, scope, address string) (Seen, error)
}
```

`now` is explicit where Python takes it, because the flush calls `due` and `claim` with **one** `moment` and the tests drive time. `scope` is a partition, never a security boundary: the host has already authenticated the connection.

### Patch semantics

`Patch` is a JSON-level deep-merge document, because that is what Firestore's `set(merge=True)` and `_merge_map` do, and callers depend on every part of it:

- maps merge per key at every depth; **omitted keys keep their old value** (43 of 239 invocations are revealed by more than one delivery);
- an **explicit null is writable** (`denials._handle` writes `{"event": {"stop_reason": ..., "user_agent": activity.actor.user_agent}}`, and a null agent clears the field);
- lists and scalars **replace** (`file_digests` is written as a replacement list);
- `Append` fields **union** (`webhook_ids`, `contributed`, `awaiting`).

`Patch` is therefore not a typed event. It is an opaque merge document with constructors for the shapes callers use: `OpenFields(event, webhookID, contributed, verdicts)`, `EventFields(event)` (reader B's standalone upsert), `Set(path, value)` / `SetNull(path)` (the denial completion), and `FileDigests(list)` plus `Append(field, values...)` (the enrichment). A `wire.Event` is converted with the `exclude_none` rule, so unset fields are omitted. Each shape has a golden case in the contract suite. Nothing outside `store/` touches a generic map; inside it the document is a JSON object, as Python's is (`record.py` stores the event "as a serialized mapping" because `parsed_as` is unknown while pending).

### Adapters

**Memory.** A mutex-guarded map; the default for unit tests above the store.

**Firestore** (`standalone/firestore`) is document-compatible with `store/gcp.py` and `platform/gcp`:

- Collection `anthropic_pending` (configurable), database `slashid-anthropic`. A created document carries `deadline`, `next_attempt_at`, `awaiting`, `attempts`, and **explicit nulls** for `claim_owner`, `claim_expires_at` and `tombstoned_at` ("an IS_NULL filter does not match a document that lacks the field"), so the Go writer must not omit nulls. `event` is a nested map; timestamps are Firestore timestamps (microsecond precision in comparisons).
- `Append` is `ArrayUnion`; expectations are cleared with `ArrayRemove`; `claim` is a compare-and-set on the snapshot's update-time precondition.
- Tombstone-only documents (retiring an address that was never written, which makes an out-of-order predecessor's upsert a no-op) have no `deadline` or `next_attempt_at`; the Go record type must tolerate their absence in `Seen` and `Claim`.
- Indexes: the Terraform provisions exactly one composite index (`tombstoned_at`, `next_attempt_at`) and a TTL policy on `tombstone_expires_at`. `nearby` uses two equality filters and the automatic single-field indexes. No new index is required.
- The tick lease is the document `tick` **in the pending collection**, fields `tick_owner` and `tick_expires_at`; release sets only `tick_expires_at` to null and only when the owner matches. Checkpoints are one document per feed in collection `anthropic_checkpoints` (`compliance_activities`, `compliance_chats`, `compliance_local_sessions`) holding `{timestamp, id}`. Both are shared state with a running Python service, so both are in the cross-implementation test.
- On the stand-alone hosts `scope` is the configured collection name (`pending_collection` for pending rows and the lease, `checkpoint_collection` for checkpoints), and Firestore document ids are unprefixed.

**SQL** (`store/sqlstore`) is the Python `SqlitePendingStore` ported, one implementation for two dialects:

- Every operation is a read followed by **one** conditional write, `UPDATE … WHERE <key> AND version = ?`; `rowcount == 0` means someone else wrote, and the operation re-reads (8 attempts, then an error). `claim` makes one attempt and returns `nil` on a lost race. Correct across processes with no transaction, so it works on a shared autocommit connection (SQLite) and on a pool (Postgres).
- `upsert` creates with `INSERT … ON CONFLICT DO NOTHING`; `Append` is unioned in Go from the row just read; `nearby` filters the time window in Go; expired tombstones are deleted by `due`.
- The stored document is `TEXT` on both dialects, not `JSONB`: Postgres JSONB renormalizes numbers and key order, which would defeat the byte-exact goal for any stored value later re-emitted.
- **Layouts.** The table and key columns are configuration, not constants:
  - the *stand-alone layout* is exactly Python's SQLite store, so an existing `SLASHID_DATA_DIR` is adopted without migration: the tables `pending(collection, address, version, doc, tombstoned_us, next_attempt_us, conversation_id, tombstone_expires_us)`, `checkpoints(collection, document, timestamp_us, id)` and `leases(collection, document, owner, expires_us)`; the same three indexes; `doc` written as Python's default `json.dumps` (ASCII-escaped, `", "` separators) with datetimes as ISO strings that Python reads back with `fromisoformat`; the four denormalised columns rewritten on every write; WAL mode, a 5-second busy timeout, a `0700` directory and the file name `data.sqlite`; and the default directory `platformdirs.user_data_dir("slashid_anthropic_forwarder", "slashid")`;
  - the *`ng-evangelion` layout* keys rows by `(connection_id, address)` and carries `org_id` (see "Tables").

  A test in `ng-evangelion` asserts the migration's columns equal the layout's, rather than comparing DDL text.

### Contract suite

`store/contract` is the Python `test_store_contract.py` ported once and run against every adapter: creation and deadline stability, the patch shapes, `Append` union, explicit-null writes, tombstone no-ops, claim exclusivity and expiry, `due` ordering and bound, `nearby` window, `FAILED` backoff, expired-tombstone cleanup, tombstone-only documents, and concurrent `upsert` on one address under `-race`.

## The hook path

The order of operations is the contract with Anthropic's circuit breaker, so it is Python's:

1. Body over the size cap → 413. (In `ng-evangelion` this check uses `Content-Length` before any buffering; see "Integration".)
2. Verify the signature against every configured secret. Failure → 401, and nothing is captured. With no secret configured every frame is rejected (Python's `verify([])` is false), except in the `ng-evangelion` no-secret mode (deviation 5).
3. Queue the raw-frame capture, if configured, as `After` work. It follows every outcome below, including unparseable and non-prompt frames.
4. Parse the frame leniently. Unparseable → `allow`, log, no record. Non-`prompt` type, unknown `type`, or `source.application == "config-test"` → `allow`, no record.
5. Build the tail event **once**. Decide the verdict. Respond.
6. `After`: write the pending records and push whatever became ready, bounded by `push_budget_ms`.

**Verdict.** The mock-hash check, then the capture marker, then preflight under `verdict_budget_ms`; the first deny in that order wins. Preflight's reasons are stripped, blanks dropped, and joined with a space, because Anthropic takes one string. A budget timeout, a transport failure, a non-200 or an unparseable body yields one fail-mode verdict; a 200 with an empty list is a real allow. The recovery sentence is appended **after** truncating the base reason (by code point) to `500 - len(recovery)`. `reference_id` is `sha256(webhook_id)[:32]`. Shadow mode keeps both the composed and the answered verdict; the record stores the answered one. Preflight is told its budget through `SlashID-Request-Timeout: max(budget - 0.5s, 0.05s)` with three decimals; the in-process `Preflighter` receives the same reduced budget and bounds itself with it.

**Silent no-record cases**, all preserved: a null `actor.id` (no events and no preflight call); an empty fresh round (no tail); a first turn (no previous-run record); a blocked verdict with no tail event.

**`After`.** One sequence, as Python's single `try` around `write_from_frame`: (1) the previous run's record, pushed if ready; (2) supersede the predecessor's tail; (3) this frame's tail, or its honoured denial. A store error in step 1 **aborts** steps 2 and 3 and is logged. That is Python's behaviour and it is kept. The context is detached from the request (`context.WithoutCancel` plus its own timeout), so a client hanging up after the response does not cancel the write.

### Failure policy

Python guards only the work after the verdict; `unanswered_round` and `decide` are not guarded, and an exception there is a 500. Go guards both: a panic or error while building the tail event or deciding is recovered, logged, counted, and answered with the **fail-mode verdict** (default allow), and `After` is still returned (it may write nothing). The only non-200s the core ever produces are 401 (bad signature) and 413 (too large).

## The tick

`Tick` takes the lease (10-minute TTL), runs the readers, then flushes, under a **hard tick deadline** (540 seconds on the `gcp` platform, matching the scheduler's attempt deadline; 10 minutes locally by default). The lease is renewed every third of its TTL while the tick runs, and a failed renewal cancels the tick, so a wedged tick cannot hold the lease and a tick never runs on a lease it lost. This differs from Python, whose lease is not renewed (deviation 3). If the lease is not held the result is `Skipped: true`.

**Readers.** Reader B (responses) before Reader A (denials), because A takes `model` from B's transcripts; each isolated, so one failing is a log line and a missing counter. The response reader's time budget (`response_reader_budget_seconds`) wraps **Reader B only**; Reader A is not bounded by it. Reader A allow-lists the activity type `inference_hooks_request_denied`, filters to the configured organization (as do the chat and session readers), and advances the activities watermark to the newest activity's timestamp with its id. Chat and session watermarks advance to `now - poll_lag`, only after a drain that completed, and never backwards (a monotonic guard). Session transcripts are read newest-first (`order=desc`) and stop at the tombstone horizon; a transcript longer than `max_transcript_messages` is emitted from its tail. A cold start pins to `now` and saves at once: no backfill. Turns older than the tombstone horizon (`now - tombstone_ttl + 5 min`) are skipped. Own `compliance_api_accessed` reads are filtered by type. The local-session listing is unorderable and drained whole under `max_sessions_per_tick`.

**Compliance client.** The query vocabulary differs per feed and is kept: activities (`created_at.gte`, `order=asc`, paged by `after_id` fed from the response's `last_id`), chats (`updated_at.gte` only with `order_by=updated_at`, `after_id`), local sessions (`updated_at.gte`, `page` fed from `next_page`, no ordering parameter). Chat messages page by `after_id` = the **last message's id**, not `last_id`. Page sizes 100, 200 (transcripts) and 1000 (chat messages); tool caps `tool_result_max_bytes` and `tool_use_input_max_bytes`. The path is `/apps/sessions/local`. The base URL is a constant, never taken from configuration or a frame (no SSRF surface). Headers `x-api-key` and `anthropic-version: 2023-06-01`.

**Flush.** For each `due` record (one `moment` for `due` and `claim`), claim for 60 seconds and push what the claim returns:

- A denial still awaiting its activity, with compliance enabled and a `deny:` address, is **discarded**: retired as superseded with a warning. This is the one place silence beats a record (v2).
- A record that fails `wire.Validate` is retired as failed and retried up to 3 attempts, then retired as superseded. Validation and push failures share **one** `attempts` counter, so a record that has also failed pushes reaches the limit sooner.
- A push error retires the record as failed, with backoff `60 s × attempts`, retried without limit, **except** a `*PermanentError` from the sink, which counts against the same 3 attempts.
- A store error inside the flush aborts the flush and the tick (as Python's `main.py` does); the tick returns its counters and the error.
- Push, then retire; a crash between them re-pushes, never loses.

## The stand-alone host

- **Routes.** `POST /tick`, where a request carrying `webhook-id` is a frame even on `/tick` (a customer's webhook URL can end in `/tick`) and anything else needs the scheduler's bearer; and `POST /{path...}` for the hook. The tick returns the counters, plus `{"flushed": 0, "skipped": true}` when the lease is not held.
- **Config.** The same `SLASHID_*` names and defaults, with all of Python's validation: at least one capability; `ORGANIZATION_UUID` required with `COMPLIANCE_KEY`; `PROJECT_ID` required on `gcp`; `push_token` non-empty; `endpoint` trailing slash stripped; `round_link_depth ≥ 1`; and the tombstone-TTL floor (`JOIN_WAIT + POLL_LAG + one tick`). Only five fields treat an empty string as unset (`compliance_key`, `organization_uuid`, `capture_bucket`, `capture_deny_marker`, `data_dir`); the rest are parsed strictly. `LOG_LEVEL` is read without the `SLASHID_` prefix. No `os.Getenv` outside `config`.
- **Platforms.** `SLASHID_PLATFORM=gcp` builds the Firestore store, checkpoints and lease and verifies the scheduler's OIDC token; `local` (the default) builds the SQLite store on `SLASHID_DATA_DIR` in the stand-alone layout and runs its own ticker, per the local design.
- **Lifecycle.** Stop accepting, drain tracked `After` work (bounded), close the platform. The local ticker sleeps one interval before its first tick, as Python's does. The local `/tick` token is compared in constant time with `tick_principal`. Boolean environment values follow the pydantic-settings grammar, case-insensitively.
- **Signature verification** wraps the Standard Webhooks library's `VerifyIgnoringTimestamp` with the tolerance check done against the injected `Clock`, because the library reads wall time and the end-to-end tests use fixed timestamps.
- **Image.** A static binary on a distroless base, `SLASHID_DATA_DIR=/data`, `VOLUME /data`, listening on `$PORT` (default 8080) and trusting forwarded headers from the platform proxy, as the Python command line does with `--forwarded-allow-ips=*`. The Terraform module is unchanged.
- **Observability.** `log/slog` JSON; the tick's counters are the `/tick` response body, as today, and also go to `Observer`.

### In-place replacement

Replacing the Cloud Run revision is safe **under one condition: the differential tests have passed**, because the two implementations then compute the same addresses and write the same documents (pending, `tick` lease and checkpoints), and `claim` is a compare-and-set that both honour. A mixed-revision window during the rollout is therefore safe. If a hash diverged, the overlap would orphan records and double-count events (dedup is on `request_id`), which is why phase 4 below is gated on the replay and shadow results and not on the mere ability to run. Rollback is the previous image against the same state.

## Integration in ng-evangelion

The logic stays in the core. `ng-evangelion` supplies ports, routes and tables, in `backend/modules/nhi/adapters/ai_forwarders/anthropic/`.

### Endpoint and request handling

`POST /ip/nhi/events/ai-invocations/preflight/anthropic`, modelled on `PostNhiEventsAIInvocationsPreflight`. The order differs from the existing preflight on purpose, because this endpoint accepts bodies of several megabytes:

1. Extract the Bearer token. Missing or malformed → 401.
2. If `Content-Length` exceeds the cap → 413 **before reading anything**.
3. `resolvePushTokenConnection` (both region guards, the 1-minute token cache). The mapping is: unknown token → 401; a connection whose source is not `anthropic` → 401 as well, so a token holder cannot probe source types; a wrong region never reaches the handler, because the edge forwards it; any other lookup failure (database error) → 200 with the organization-independent default verdict (`allow`), counted, because before a connection exists there is no per-connection fail mode.
4. Only now read the body, with a hard cap (default **8 MiB**; measured frames reach 1.9 MiB, and the Python cap of 32 MiB is not carried over) and a process-wide semaphore on bytes held. When `Content-Length` is absent (chunked), the semaphore reserves the whole cap up front, so unauthenticated or concurrent callers cannot exhaust memory.
5. Load the `Forwarder` for the connection (cache below), `HandleFrame`, respond; run `After` on the adapter's group.

**Status codes.** A non-200 is a webhook failure to Anthropic and feeds its circuit breaker. The handler therefore answers only 401 (bad token, wrong source, or bad signature) and 413 (too large) with a non-200. A database error, a decrypt failure or a cache miss that cannot be filled while resolving the credential answers the fail-mode verdict with a 200 and is counted. A wrong region is not an error here: the edge routing forwards the request to the org's region before the handler runs, which is why the route must be registered there (below).

### Gate and edge routing

The gate route (`deploy/terraform/gate.hcl`, order 27270) is **one global route** to the external Cloud Run service. Retargeting it moves every request for that path to `um`, where a request without a Bearer header gets 401. That service is a single deployment with one baked-in push token, serving one connection, and its target is marked provisional; so the cutover is one deliberate step for that connection, not a flag. To keep it safe:

- the integrated endpoint is first reachable on a **separate path** (`…/preflight/anthropic/v2`) with its own gate route, so customers move one at a time by editing their hook URL and adding the Bearer header;
- route 27270 stays pointed at the legacy service until no hook uses it, and is then retargeted to `um` and the `/v2` route removed.

The new path must also be registered where every push-ingestion operation is enumerated, or a multi-region request is not forwarded to its org's region before the handler runs:

- `backend/common/http/middleware/edge_region_routing.go`: `fixedPushRoutes` gets a `BearerPushToken` entry;
- `backend/common/http/ipplane/headerless.go`: a `DispositionPushIngestion` entry;
- both the endpoint and its `/v2` twin are declared in `spec/openapi.yaml`, in `HeaderlessOperations` (which must equal the spec's header-less set exactly, per its own comment) and in `fixedPushRoutes`; the tests that pin the push-ingestion count (6 today, in `headerless_test.go`, and the count comment in `headerless.go`), the push-channel edge-route coverage test and the gate-route check script are updated in the same change, and updated again in phase 7 when `/v2` is removed.

`request_validator` stays disabled for these routes, as for the siblings.

### Credential, settings and the Forwarder cache

`AnthropicConnectionCredential` gains one optional field, `hook_signing_secrets []string` (rotation = two entries), with `Validate` ("positions only, never the key material"), `AnthropicConnectionCredentialUpdate` handling, the API schema and clients, and the same redaction in API responses as `api_keys`. Hook-only connections do not exist in `ng-evangelion`: `Validate` requires at least one API key, and the connection's API keys are what the reader uses.

The core `Config` is assembled by the adapter:

| `Config` | Source |
| --- | --- |
| `HookSigningSecrets` | `cred.HookSigningSecrets`. The signature is verified whenever any are configured; with none, the Bearer token alone authenticates (see Decisions needed) |
| `ComplianceKeys` (per feed) | the existing resolver picks the winning key per feed's scope family; chats and sessions use a **new family** `compliance_conversations` (`read:org_audit`, or `read:compliance_user_data`), because the existing `compliance_directory` and `activity_feed` families do not cover them |
| `OrganizationUUID` | `cred.OrganizationID` |
| `Endpoint`, `PushToken` | unused: sink and preflight are in process |
| wire-affecting settings: `IncludeRawContent`, `MaxContentSize`, `InputScope=round`, `RoundLinkDepth=10` | Python's defaults; they change hashes and payloads, so they are explicit, not implied |
| verdict settings: `PreflightEnabled=true`, `ShadowMode`, `VerdictFailMode`, `VerdictBudgetMs`, `MockDeniedHashes`, `CaptureDenyMarker` | Preflight is always on (in process). `ShadowMode` defaults to **true** and `VerdictFailMode` to **allow**, as in Python (observe before enforce); they are per-organization dynamic configuration. The mock-hash and marker affordances are test-tenant only and off |
| thresholds (`JoinWait`, `PollLag`, `TombstoneTTL`, …) | Python defaults with the same validators |

Resolving the credential per frame would put a database read and a decrypt on the 3.5-second verdict path (the existing preflight resolves from a token cache only). The adapter therefore caches the built `*Forwarder` per connection id for **60 seconds** (the token cache's horizon). On expiry it re-reads and decrypts the credential and compares a hash of the secrets with the cached one, rebuilding only when it changed. There is no credential version field, so rotation takes effect within a minute. On a miss that cannot be filled, see "Status codes".

When `organization_id` is set, a frame whose `tenant_id` differs is a tenant-binding violation. v2 measured `tenant_id` equal to the Compliance `organization_uuid` (and not the id the claude.ai settings page shows, which matches nothing). It is logged and counted, and behind a flag (default off) answered `allow` with no record.

**Isolation argument.** A Bearer holder for organization A can only be verified against A's signing secrets, so cross-tenant spoofing is blocked by the per-connection HMAC, but only for connections that have secrets configured (an optional, per-connection setting). Without them the Bearer token is the whole credential, the same trust the other push endpoints have; the tenant-binding check is likewise off by default. Making the secrets mandatory is Decision 2.

### Preflight and sink, in process

- **Two fail knobs, composed.** `AIPreflight` already turns a server-side check that cannot complete into a deny or allow reason per organization (`preflightDenyOnError`). `VerdictFailMode` covers only what that cannot: our own budget expiry, an internal panic, or a transport failure. The two do not overlap, and the endpoint documents which applies where.
- `Preflighter` calls the preflight logic with the already-resolved connection. `AIPreflight` takes `(ctx, authToken, invocation)` and resolves the token itself, a second lookup on the critical path, so it is split into `aiPreflightFor(ctx, conn, invocation)` with `AIPreflight` as a thin wrapper. The wire event is converted to `aievent.AIInvocationObservedV1` by a JSON round trip, pinned by a conformance test.
- `Sink` reproduces `GenerateUnprocessedEvents` for each event, on a copy: the event is an `InvocationEnvelope`, its `OrgID` and `ConnectionID` are stamped, an empty `request_id` gets a UUID, and **an empty or null `identity_details` is rejected**, because the Anthropic identity resolver downstream treats it as a permanent error. Then `ip_events.NewUnprocessedPushedEventV1(orgID, connID, sourceName, sourceIdentifier, EVENT_INGESTION_CHANNEL_AI_INVOCATION, payload)` and `ipPublisher.Publish`. The detached context uses `context.WithoutCancel` and `db.StripUserMark`, with a `recover`, as the other background code in `ng-evangelion` does.

### Background work and shutdown

`um` has no shared "run after the response" facility; handlers spawn goroutines with a detached context, and the HTTP server's `Shutdown` waits for requests, not for them. The adapter owns a tracked group for `After` work. The shutdown plan in `service/bus_shutdown.go` runs its intake steps **concurrently**, and the HTTP server is shut down by a separate goroutine that the plan does not order, so no step can be sequenced "after the server stops". The group therefore enforces its own ordering: once closed it refuses new work (a frame that arrives late still gets its verdict, and its `After` work is dropped and counted), and its drain step, registered as an **intake** step, closes the group first and then waits for work already in flight. Intake completes before the publisher steps of the later phase, so in-flight work can still publish. The intake budget is `ShutdownDrainBudget`, 8 seconds, inside Cloud Run's 10-second SIGTERM-to-SIGKILL window; work that does not finish is lost, and the design tolerates it: the next frame's emit-previous reports the previous run, and the deadline flush covers the rest.

### The tick job

A per-connection Cloud Scheduler job, following channel renewal and run-detections:

- `ConnectionsScheduler` gains `ScheduleAIIngestTickForConnection` and `StopAIIngestTickForConnection`, implemented for GCP, Docker and the failing scheduler, with the mocks regenerated.
- The job `POST`s `/internal/nhi/ai-ingest-tick/{connection_id}` with the `HeaderOID` header, handled like `PostInternalNHIRenewChannelsConnectionId`: `GuardNHIOrgRegion`, load the connection (decrypted), reject non-anthropic connections, call `Tick`, record the counters, and **self-heal on not-found** by stopping its own job, as `RenewChannelsForConnection` does. A revoked-key 401/403 from Anthropic becomes a `SyncBlockError` so the customer sees the reason.
- **Where it is scheduled and stopped.** `StartSynchronization` returns early when `!CredentialSupportsPullSync` and schedules only the sync and channel renewal, so the new job gets its own hook beside `scheduleChannelRenewal`, and the resume and credential-update paths call it too. It must be stopped at every site that stops the others: `StopSynchronization`, the gone-connection self-heal in `SynchronizeConnection` (both branches) and `stopChannelRenewalJobOfGoneConnection`, and the connection-delete and org-teardown paths. Org teardown has its own list: `SchedulerTeardownOps` (`orgteardown/ops.go`) stops three jobs per connection and keeps the sync job as the retry anchor, so the new stop method is added to `ConnectionJobsStopper` and to that op and runs **before** the sync job's stop, as the self-heal does; the planned-row count in that op's documentation changes. The start side uses `NHIServesOrgHere` while the stop side uses the global data region (a documented TODO); the new job inherits that asymmetry.
- **The flag has no reconcile path** (connection lifecycle events are the only schedule triggers), so the job is scheduled for every anthropic connection, and the tick endpoint checks `anthropic_ai_ingest_in_process` first and returns immediately when it is off. Turning the flag on later therefore needs no scheduling event. If the Cloud Scheduler quota binds (Decision 4), a reconciler on flag change replaces this.
- **Cadence** every 5 minutes, as Python. The lease guards against overlap from a retry (the job has `RetryCount: 1` and a 30-minute attempt deadline), a slow tick, a deploy overlap and a manual trigger.
- **Scale.** One Cloud Scheduler job per Anthropic connection; the project quota per location has not been checked (see Decisions needed).

Why not `SourceEventPoller`: it runs once a day at a random minute after a full sync that must succeed, keeps one timestamp per connection, and collects every event in memory before publishing. None of that fits a minute-scale reader with three watermarks and a lease. The existing activity-feed poller stays unrelated.

### Tables

A `postgres_sync` migration follows the house pattern of `0411_ip_connection_handler_progress.sql`: per-connection rows carry `connection_id uuid REFERENCES external_identities_connections(…) ON DELETE CASCADE` and `org_id`, reads carry the org predicate (the `orgscopecheck` lint), and the cascade is what org teardown relies on. This matters more than usual, because `doc` holds customer transcript text. Three tables: pending records, checkpoints (three rows per connection), and the tick lease (one row per connection). The migration is registered with the repository's migration workflow (db model regeneration and the migration-id check), and the three tables are added to the **residue-purge registry** (`cmd/nhi-residue-purge/residuepurge/registry.go`, the list of connection-keyed tables, with its registry and schema tests). The SQL adapter is configured with this layout, and `org_id` is a **layout field fixed per `Forwarder`**, not an argument of `Store`: every statement the adapter builds carries the connection and org predicates. Because `sqlstore` lives in another module, `ng-evangelion`'s `orgscopecheck` lint cannot see its SQL, so a test in the adapter asserts that every statement the layout produces carries the org predicate.

### Spec and generated code

- `spec/openapi.yaml`: the new paths (`…/preflight/anthropic` and the transition `/v2`) with an `AnthropicHookVerdict` response (`{action, deny_reason, reference_id}`); regenerate the dependent specs, server and client code. The internal tick endpoint likewise.
- `spec/ai-schemas.yaml`: the batched extensions the convention defers (`AIAccessedFile.provenance`, `AnthropicIdentityDetails` fields), `additionalProperties: false` on the outbound envelope types, and the GCP-variant required-field gap found while prototyping. These tighten validation for the Python forwarders too.
- `deploy/terraform/gate.hcl`: the `/v2` route, later the retarget of 27270.
- Dynamic config: `anthropic_ai_ingest_in_process` per organization gates whether the endpoint serves and whether the tick job is scheduled; per-org `shadow_mode` and `verdict_fail_mode`.

### Security

Frame bodies, Bearer tokens and signing secrets are never logged; the raw-frame capture sink holds full frames and stays off for production tenants. The Compliance base URL is fixed. Credentials are never part of an error message. Per-connection stores are partitioned by connection id with the org predicate on every read.

## Cutover from stand-alone to integrated

State differs (Firestore versus Postgres), and a cold start in the integrated service pins to `now` and never backfills, so moving a customer follows a fixed order. The point of the order is that the integrated reader's starting state must be copied **after** the stand-alone service has stopped producing, or it re-reads and re-emits the window stand-alone had already handled:

1. **Prepare.** Deploy the integrated endpoint and tick job for the connection with the stand-alone service still live. The integrated forwarder runs with a **recording sink** (a flag on the adapter that writes would-be events to a table and publishes nothing), so nothing is pushed twice; any checkpoints it writes now are discarded in step 4.
2. **Stop the old producer.** Switch the customer's hook (new URL, Bearer header) and pause the stand-alone scheduler. The stand-alone service stops reading feeds and receiving frames; its remaining pending records are drained by letting its flush run (bounded by `join_wait`, 1 hour by default, so the last tick is triggered manually or the wait is accepted), until the pending collection is empty.
3. **Final copy.** A migration tool (`standalone/cmd/anthropic-migrate`) copies the three **checkpoints** and the **unexpired tombstones** from Firestore into the Postgres tables, overwriting what step 1 wrote. With the tombstones copied, the integrated reader cannot re-emit a turn the stand-alone service already handled.
4. **Go live.** Disable the recording sink. The integrated tick pushes from the copied state.

Pending records are drained, not migrated: they hold in-flight state keyed to the stand-alone store. Between steps 2 and 4 hook frames are received by the integrated endpoint and recorded, not pushed; the pending writes land in Postgres and are flushed in step 4. The remaining double-push window is covered by address parity, which is why the differential tests gate this.

## Testing

**Everything above the ports is tested in this repo, in seconds, with no `ng-evangelion` checkout.** The adapter has a small suite of its own (credential mapping, sink payload shape and rejection of empty identity, handler status codes, the body being read only after authentication, the tenant-binding flag).

1. **Golden corpus from Python, built inside the production image.** A script under `tools/golden/` runs the real pipelines over: every fixture frame in `anthropic/tests/fixtures`; the compliance fixtures; scripted transcripts for unions, non-ASCII, lone surrogates, `NaN`, big and float numbers, attachments and tool results; and property-generated transcripts. It writes the full `AIInvocationObservedV1` for each pending-record state, every address, canonical-JSON bytes, outputs of `truncate_middle`/`strip_cat_n`/`guess_type`/`shlex`/`strip`, and the store documents Python would write. Goldens are committed under `go/forwarder/testdata/`, regenerated by one command, never edited by hand.
2. **Existing YAML cases.** The Go suite reads the existing `test_*.yaml` files (`split_transcript`, `joinable_address`, `partial_event`, `write_from_frame`, `decide`, `produced_runs`, …) through a small loader, so shared cases are literally the same files.
3. **Differential property tests** for `canon`, `textutil`, `reads`, `address` and `wire.Validate`: random inputs hashed by both implementations, compared byte for byte.
4. **Store contract suite** over memory, SQLite, Postgres (a service container) and Firestore (the emulator), plus the cross-implementation test: Python writes and Go reads, and Go writes and Python reads, for pending documents, the `tick` lease and the checkpoints.
5. **Race and fuzz.** `-race` throughout; fuzz targets on the frame parser and union decoders.
6. **Spec contract test** (optional job): golden events validated against `ng-evangelion/spec/ai-schemas.yaml`.

### Replay and shadow

- **Offline replay (before any live run).** The Python service already captures raw frames with headers (`capture_bucket`). A recorder is added to Python's `ComplianceClient` for the Compliance exchanges. A harness replays frames and recorded exchanges through **both** implementations with a fixed clock, a stub preflight and a recording sink, and diffs the emitted events and store documents. This is deterministic and touches nothing live.
- **Live shadow (isolated).** A Go instance receives a copy of live frames from a tee in the Python service (a flag that forwards raw body and headers to a shadow URL, off by default) and runs against **its own collection, database or tables, checkpoints and lease**, a recording sink, and a stubbed preflight (its verdicts are discarded and never reach Anthropic). It must never share state with the live Python service: sharing would let Go claim and tombstone records Python depends on, advance the shared checkpoints and take the shared lease. A comparison job joins on `request_id` and reports equal, differs (with the paths), or missing on one side; the accepted deviations are a separate category.
- **Exit criteria.** Zero differing events over a week that includes a Claude Code session, a claude.ai attachment and a denied request, with misses explained by window edges.

## Rollout

| Phase | Deliverable | Exit criterion |
| --- | --- | --- |
| 0 | Spec hardening in `ng-evangelion` (`additionalProperties`, GCP variant, the batched extensions) | Merged; Python forwarders unaffected |
| 1 | `core`: `canon`, `textutil`, `wire` (with validate-at-push), `normalize`, `rounds`, `reads`, `mediatype`; the golden harness | Golden corpus green; differential tests green over at least 1M cases |
| 2 | `anthropic` against the memory store, plus the stand-alone host on SQLite | Python's anthropic cases ported and green; local end to end green |
| 3 | Firestore store, checkpoints and lease, the cross-implementation test | In-place cutover rehearsed on the test project |
| 4 | Offline replay, then isolated live shadow; then replace the Cloud Run revision | Exit criteria above; rollback is the previous image |
| 5 | `ng-evangelion` adapter: endpoint (`/v2`), edge registries, credential field, `compliance_conversations` family, tables, tick job, flag | Flag on for a test org; hook and reader verified end to end |
| 6 | Integrated shadow, then customer-by-customer cutover per the sequence above | Same criteria; each customer edits hook URL and header once |
| 7 | Retarget route 27270, remove `/v2`, retire the Cloud Run service for moved customers; keep the stand-alone image for users outside `ng-evangelion` | No hook uses the legacy target |

Rough size, to be refined at planning: about 7–9k lines of Go plus tests for phases 1–4 (the Python is about 4k lines of service code plus about 2.5k of the shared code it reaches). The `ng-evangelion` side is larger than the adapter alone suggests: credential API and clients, edge registries and their tests, scheduler implementations and mocks, the migration and dynamic config, and tests across them. Plan for roughly 2–3k lines, not under 1k.

## Risks

- **Silent hash divergence** is the worst failure and the likeliest class of bug (Unicode, numbers, surrogates, normalization versions). Mitigation is structural: the golden corpus and differential tests gate phase 1; replay and isolated shadow gate every live step.
- **Dedup is first-completed-wins.** Both implementations pushing for one connection with different `request_id`s double-counts. Only one pushes per connection; shadows use a recording sink.
- **Customer reconfiguration** for the integrated hook (new URL, Bearer header); stand-alone stays available unchanged.
- **Large bodies in `um`.** Mitigated by authenticate-then-read, the 8 MiB cap and the byte semaphore.
- **Verdict latency in `um`.** The budget is 3.5 s of Anthropic's 5 s. In-process preflight removes a hop but `um` is a shared process; a verdict-latency histogram from day one, and the fail mode as the safety net.
- **Compliance API rate limit** (600 requests/minute per parent organization) is shared with the directory sync and activity poller, which do not coordinate with the reader.
- **Parallel maintenance** until Python is retired. Any change to a ported Python module must regenerate the golden corpus in the same PR; a CI check fails when `shared/**` or `anthropic/**` change without a corresponding `testdata` change.
- **Go ecosystem gaps:** no Pydantic. The cost is boilerplate in the union helpers, `Rows` and `NonNull`, contained to a few types and covered by the corpus.

## Beyond Anthropic

`go/forwarder/core` is the part the other forwarders would reuse: `wire`, `canon`, `rounds`, `normalize` (Converse, Gemini and OpenAI Responses normalizers would be added there), `pushclient`, and the platform ports. Nothing here commits to them.

## Decisions needed

1. **Gate transition.** Is the existing hosted deployment really one connection, and is a per-customer `/v2` path acceptable? The alternative is a one-step retarget of 27270 for that connection.
2. **Is the HMAC optional in `ng-evangelion`?** The design verifies it when secrets are configured and otherwise accepts the Bearer token alone. Making it mandatory matches Python but requires every customer to store the signing secret in SlashID.
3. **Tenant-binding check:** log-only by default (proposed), or enforce.
4. **Per-connection scheduler job or one fan-out job.** Per-connection matches the pattern and isolates failures; the Cloud Scheduler quota per location needs checking against the number of Anthropic connections.
5. **Scope claim for `compliance_conversations`:** that chats and sessions are served by `read:compliance_user_data` alone comes from v2 measurements and should be confirmed with a key holding only that scope before the resolver change merges.
6. **Where `shadow_mode` and `verdict_fail_mode` live.** Proposed as per-organization dynamic config; the alternative is per-connection non-secret settings on the connection.
7. **Postgres in this repo's CI** (a service container on GitHub Actions) versus running the Postgres contract run only from `ng-evangelion`'s CI.
8. **Vendoring and nested tags.** `postgres-outbox` and `ratelimit` are repository-root modules; a nested module tagged `go/forwarder/vX.Y.Z` has no precedent in `ng-evangelion`. Confirm it resolves through `GOPRIVATE`, the vendoring flow (`go-mod-vendor`, a git-ignored `vendor/`) and the Dockerfile's authentication.
