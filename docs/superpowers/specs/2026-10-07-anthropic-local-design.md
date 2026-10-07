# Anthropic forwarder on the local platform

**Date:** 2026-10-07
**Status:** Design, ready for planning.
**Target repo:** `slashid-ai-forwarders`, `anthropic/` (`config.py`, `platform.py`, `main.py`, `store/local.py`, `Dockerfile`, `README.md`).
**Follows:** `LocalPlatform` (#80), which deferred this ("the Anthropic `PendingStore` on SQLite (on hold)").

## Problem

The Anthropic forwarder runs only on GCP: Firestore holds the pending records and Cloud Scheduler drives `/tick`. A customer who does not run on GCP cannot use it.

## Goals

1. `SLASHID_PLATFORM=local` runs the same FastAPI app from the same image on a container with a volume: no Google SDK is loaded, no Cloud Scheduler exists.
2. The process drives its own ticks on a timer.
3. Pending records live in the same SQLite database as the checkpoints and the lease.
4. GCP behaviour is unchanged.

## Non-goals

Ingress: the customer exposes the container to Anthropic over public HTTPS, and the README says so. Terraform or Helm for any non-GCP cloud. Other adapters (Vertex's sources are GCP-specific; Codex is local already).

## Design

### Config

- `platform: Literal["gcp", "local"]`.
- `project_id` becomes `str | None`; a validator requires it when `platform == "gcp"`, so the existing "project id is required" behaviour holds there.
- `data_dir: str | None` (`SLASHID_DATA_DIR`): the state directory. Unset, the registry's default applies, `user_data_dir("slashid_anthropic_forwarder", "slashid")`. The image sets it to a volume path.
- `tick_interval_seconds` already exists (default 300) and already feeds the tombstone-TTL floor; locally it is also the timer's period. `tick_principal` locally is the expected bearer token for `/tick`.

### Wiring (`platform.py`)

`open_backends` passes the options of the configured platform: `project`/`firestore_database` for `gcp`, `app`/`path` for `local`. `_pending_store` gains a `LocalPlatform` arm that builds `SqlitePendingStore(db=platform.sqlite, ...)`. The module still imports no cloud SDK at top level: `GcpPlatform` is imported where it is matched.

### The timer (`main.py`)

The body of `/tick` after authorization moves into a `run_tick()` closure in `create_app`: the lease, the reader pass, `flush_due`, the counters. The route calls it, so GCP is unchanged. When `config.platform == "local"`, the lifespan starts one task after the backends open:

```python
while True:
    await asyncio.sleep(config.tick_interval_seconds)
    try:
        await run_tick()
    except Exception:
        log.exception("tick failed")
```

No overlap handling: a tick that cannot take the lease reports `skipped`, which is what makes several workers or replicas safe. The task is cancelled and awaited on the way out, before the platform closes. `run_tick` already catches the reader pass; the `except` is for the flush and the lease. `/tick` stays mounted and guarded by `tick_principal`, which for the local platform is a shared secret; unset, it refuses every request and the timer is the only driver.

### `SqlitePendingStore` (`store/local.py`)

One table in the shared database, created by the store (`CREATE TABLE IF NOT EXISTS`) when it is built:

```
pending(collection, address, version, doc,
        tombstoned_us, next_attempt_us, conversation_id, tombstone_expires_us,
        PRIMARY KEY (collection, address))
```

`doc` is the JSON of the record's fields, exactly what the Firestore store writes (`deadline`, `awaiting`, `attempts`, claim fields, `event`, ...), datetimes as ISO strings. The four columns beside it are copies of the fields a query needs, kept in step by every write; indexes on `(tombstoned_us, next_attempt_us)` and `(conversation_id)`. Timestamps are integer microseconds, as in the other local stores.

**Concurrency.** The connection is shared with the checkpoint store and the lease, and it is autocommit with every statement committing by itself, so a multi-statement transaction would interleave with theirs. Every operation is therefore a read followed by a single conditional write, the SQLite form of Firestore's `update_time` precondition:

- `UPDATE ... SET doc = ?, version = version + 1, ... WHERE collection = ? AND address = ? AND version = ?`; `rowcount == 0` means someone wrote in between.
- `upsert` creates with `INSERT ... ON CONFLICT DO NOTHING` (`rowcount == 1` is `created`), and otherwise merges under the check. `Append` fields are unioned in Python from the row just read, so two writers extending one field cannot lose each other's values: the loser retries from a fresh read. A bounded retry loop (a handful of attempts) covers `upsert`, `complete` and `retire`; exhausting it raises, and the callers already log and carry on.
- `claim` does not retry: a lost compare-and-set returns `None`, as Firestore's `FailedPrecondition` does.

This is correct across processes (WAL, atomic statements) as well as across coroutines.

**Operations**, with the Firestore store's semantics:
`upsert`, `complete`, `claim`, `retire` and `seen` as in `FirestorePendingStore`, including: creation sets `deadline = next_attempt = now + join_wait` and never moves it on merge; a tombstoned address is a no-op that says so; `complete` never creates; `claim` returns the record as it is now and does not check readiness; `FAILED` bumps `attempts`, clears the claim and sets `next_attempt = now + retry_backoff * attempts`; the other outcomes tombstone with `tombstone_expires = now + tombstone_ttl`.
`due` selects `tombstoned_us IS NULL AND next_attempt_us <= ?` oldest first, bounded. `nearby` selects live rows of the conversation and applies the time window in Python, for the reason the Firestore store gives (two spellings of one instant).
**Tombstone expiry.** Firestore's TTL policy deletes expired tombstones; SQLite has none. `due` first runs `DELETE ... WHERE tombstone_expires_us <= now`: once per tick, indexed, and it needs no second scheduler. A tombstone outlives the window in which a late reader could re-emit its invocation, so deleting only the expired ones preserves the guarantee `tombstone_ttl_seconds` already states.

The shared `record.from_document` and `Append` are reused unchanged.

### Packaging and docs

- `anthropic/pyproject.toml` depends on `slashid-ai-forwarder-core[local]` as well as the GCP extra it uses today, so the one image serves both; the Dockerfile's `uv sync` follows, and `uv.lock` is regenerated. The image sets `SLASHID_DATA_DIR=/data` and declares `VOLUME /data`.
- README: a "Running without GCP" section: the environment variables (`SLASHID_PLATFORM=local`, `SLASHID_DATA_DIR`, `SLASHID_TICK_INTERVAL_SECONDS`, `SLASHID_TICK_PRINCIPAL` if `/tick` is wanted), a `docker run` with a volume, the requirement that Anthropic reach the webhook URL over public HTTPS (provided by the customer), and a note that one replica is the supported shape (several are safe but share one file, so they must share a volume).

## Testing

- **Store:** the Firestore store's test cases, run against both stores through one parametrized fixture where the behaviour is shared (creation, deadline stability, merge and `Append` union, tombstone no-ops, claim exclusivity and expiry, `due` ordering and bound, `nearby` window, `FAILED` backoff). SQLite-only cases: a lost compare-and-set on `claim` returns `None`; two interleaved `upsert`s extending one `Append` field both survive; expired tombstones are deleted by `due` and live ones are not; the schema is created once and idempotently; a second store on the same file sees the first one's records.
- **Config:** `local` needs no project id; `gcp` still does; `data_dir` is optional.
- **Wiring:** `open_backends` with `platform="local"` and an in-memory path yields a `SqlitePendingStore` and imports no Google module (assert on `sys.modules`).
- **Timer:** with a short interval and a stub `run_tick`, ticks fire repeatedly; an exception in one does not stop the next; the task is gone after shutdown; `platform="gcp"` starts no task.
- **End to end:** a `create_app(local)` test: deliver a signed frame, advance the clock past the deadline, let the timer tick, and see the event pushed to a fake sink and the record tombstoned.

## Risks

- **One connection, one thread.** aiosqlite serializes statements; `due` and `nearby` on a large backlog delay the others. The pending set is bounded by `join_wait` and the per-tick flush limit, so this is acceptable; revisit with the platform's own note.
- **State on a volume.** Losing it loses unflushed records, the watermarks and the tombstones, so a late reader can re-emit an invocation. The README says to back the volume.
- **Retry exhaustion** in the compare-and-set loop is possible under pathological contention and surfaces as a logged failed write, the same class as any storage error today.
