# LocalPlatform: the platform seam on SQLite

**Date:** 2026-10-01
**Status:** Design, ready for planning.
**Target repo:** `slashid-ai-forwarders`, `shared/` (`platform/local/`, `platform/__init__.py`, `pyproject.toml`).
**Follows:** the platform seam (#69) and the async checkpoint store (#77).

## Problem

The only platform is `gcp`, so nothing that uses the seam can run, or be tested, without Google. A local run (development, a Codex collector) wants checkpoints, a tick lease and blobs in a file; a test wants the same in memory.

## Goals

1. `LocalPlatform` implements `Platform` on SQLite: `checkpoint_store`, `tick_lease`, `blob_sink`, `scheduler_auth`.
2. It runs on a file or on `:memory:`. The caller chooses where: the platform has no default location.
3. It exposes the database as a property, the way `GcpPlatform` exposes `firestore`, so an adapter with state of its own can share the connection.

## Non-goals

The Anthropic `PendingStore` on SQLite (on hold), `platform: "local"` in any adapter's config (Anthropic needs the pending store; Vertex's sources are GCP-specific), and replacing the Firestore fake in existing tests. The platform is built and tested on its own.

## Design

### Shape

`platform/local/__init__.py` holds `LocalPlatform`; the stores sit beside it, as `platform/gcp/firestore.py` does. `platform/__init__.py` registers it: `_PLATFORMS["local"] = "slashid_ai_forwarder_core.platform.local:LocalPlatform"`, so `platforms.get("local", path=…)` works and nothing loads `aiosqlite` until asked. A `[local]` extra on `shared` carries `aiosqlite`.

```python
LocalPlatform(path: str | Path)
```

### Where the data lives

`path` is required: a file path, or `":memory:"`. Where a file belongs (a user data directory, a state directory, a temporary one) is the caller's decision, so the platform reads no environment and imports no directory library. For a file, the platform creates the parent directory on first use.

### The database property

`LocalPlatform.sqlite` is the cached `aiosqlite.Connection`, built without I/O and started on first use by the platform, as `GcpPlatform.firestore` is built without I/O. One connection serves every store: an in-memory database exists per connection, so sharing is what makes `:memory:` work, and a file database gets the same behaviour.

aiosqlite cannot start a connection twice, so the platform owns the start: an internal `async def _open()` starts it once under a lock and applies the pragmas (`journal_mode=WAL` and `busy_timeout` for a file; `foreign_keys=ON` for both) and creates the tables. Every interface method awaits it first. A caller using the property directly awaits `platform.open()`, which returns the started connection. `aclose()` closes it, and `async with LocalPlatform(...) as platform` opens and closes.

### The interfaces

Times are stored as integer microseconds since the epoch (UTC), which is exact and comparable.

- **`checkpoint_store(collection, document)`:** table `checkpoints(collection, document, timestamp_us, id, PRIMARY KEY (collection, document))`. `load` returns `Checkpoint(None, None)` when the row is absent; `save` upserts. A naive `datetime` is treated as UTC, as `FirestoreCheckpointStore` does.
- **`tick_lease(collection, document)`:** table `leases(collection, document, owner, expires_us, PRIMARY KEY (…))`. `hold(duration)` takes the lease with one statement inside `BEGIN IMMEDIATE`: insert if absent, or update where `expires_us <= now`, and report whether this owner now holds it. It yields `True` or `False` and, when held, clears `expires_us` on exit only if `owner` is still this one, so a lapsed lease taken by someone else is left alone, as in `FirestoreTickLease`.
- **`blob_sink(bucket)`:** table `blobs(bucket, name, content_type, data, PRIMARY KEY (bucket, name))`, and `put` replaces. Blobs live in the database rather than as files so that `:memory:` works and a run has one file of state. The sink also has `get(name)` for tests and tools; it is not part of the `BlobSink` protocol.
- **`scheduler_auth(principal, audience)`:** `principal` is the expected bearer token; a token is accepted if it equals it (`hmac.compare_digest`). An unset principal refuses everything, as the protocol requires. `audience` is ignored: there is no token to carry one.

## Testing

- `LocalPlatform(":memory:")` for every test, inside `async with`.
- One suite for `checkpoint_store` and `tick_lease` that runs against both `GcpPlatform` (with the existing async Firestore fakes) and `LocalPlatform`, so they cannot drift: first-load empty, round trip, naive datetime, separate documents, separate collections; lease taken, refused while held, taken after expiry, released by its owner, not released by a stranger, released on an exception.
- Blobs: put, replace, get, buckets are separate. Scheduler auth: accepted, wrong token, unset principal.
- Location: a file path in a temporary directory, including one whose parent does not exist yet (created on first use), and `:memory:`; a second `LocalPlatform` on the same file sees the first one's data.
- Lifecycle: the connection starts once under concurrent first use, `aclose` is safe twice, and using a closed platform raises clearly.
- `platforms.get("local")` resolves; `platforms.get` on an unknown name lists `local` among the known.

## Risks

- **One connection, one thread.** aiosqlite serializes statements on one worker thread, so a slow statement delays the others. That is fine for checkpoints, a lease and blobs; a future pending store with heavier queries is a reason to revisit it.
- **Two processes on one file.** WAL and `BEGIN IMMEDIATE` make the lease correct across processes; the busy timeout bounds the wait.
