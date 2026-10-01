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

`LocalPlatform.sqlite` is the cached `aiosqlite.Connection`, built without I/O (`aiosqlite.connect(path)` does nothing until awaited) the way `GcpPlatform.firestore` is built without I/O. One connection serves every store: an in-memory database exists per connection, so sharing is what makes `:memory:` work, and a file database behaves the same way.

aiosqlite starts a connection by awaiting it, and awaiting a started one raises, so the platform owns the start. An internal `async def _open()` starts it once, creating the lock lazily on first call (a lock built in `__init__` would be bound to whatever loop was current then), and sets up the file:

- for a file path, create the parent directory (`mkdir(parents=True, exist_ok=True, mode=0o700)`; SQLite cannot create it) and set `journal_mode=WAL` and a busy timeout (SQLite's default is already five seconds, so set it only to name the value); for `:memory:` skip both;
- open the connection with `isolation_level=None`, so every statement commits by itself and no implicit transaction is ever left open for a later one to trip over;
- create the tables (`CREATE TABLE IF NOT EXISTS`).

Every interface method awaits `_open()` first. A caller using the property directly awaits `platform.open()`, which returns the started connection, and never uses the connection itself as a context manager (`async with connection` awaits it, which fails once started). `LocalPlatform` is itself an async context manager, whose `__aenter__` is `open()` and `__aexit__` is `aclose()`; `aclose()` is idempotent and marks the platform closed, and any later use raises `RuntimeError("LocalPlatform is closed")`.

The worker thread aiosqlite starts is not a daemon, so a platform that is never closed keeps the interpreter from exiting. `aclose()` (or the context manager) is therefore part of the contract, and every consumer closes it.

### The interfaces

Times are stored as integer microseconds since the epoch (UTC), converted back with `EPOCH + timedelta(microseconds=us)`, which is exact. A timezone-aware `datetime` is converted to UTC and a naive one is taken as UTC, as `FirestoreCheckpointStore` does. Every operation below is a single statement, so none needs a transaction.

- **`checkpoint_store(collection, document)`:** table `checkpoints(collection, document, timestamp_us, id, PRIMARY KEY (collection, document))`. `load` returns `Checkpoint(None, None)` when the row is absent; `save` is `INSERT … ON CONFLICT DO UPDATE`.
- **`tick_lease(collection, document)`:** table `leases(collection, document, owner, expires_us, PRIMARY KEY (collection, document))`. `LocalTickLease` has the same public `take(lease, *, owner, now=None)` and `release(*, owner)` as `FirestoreTickLease`, and `hold(duration)` calls them, so the same tests can drive both. `now` is computed in Python and bound as a parameter, never SQLite's own clock. `take` is one atomic upsert, `INSERT … ON CONFLICT(collection, document) DO UPDATE SET owner = ?, expires_us = ? WHERE expires_us IS NULL OR expires_us <= ?`, and it holds the lease when `rowcount == 1`; a released lease has a NULL expiry, hence the `IS NULL` arm. `release` is `UPDATE … SET expires_us = NULL WHERE collection = ? AND document = ? AND owner = ?`, which does nothing, and raises nothing, when the lease has lapsed and been taken by someone else or the row is gone. `hold` yields `True` or `False` and releases on every way out of the block when it was held.
- **`blob_sink(bucket)`:** table `blobs(bucket, name, content_type, data, PRIMARY KEY (bucket, name))`; `put(name, data, *, content_type)` replaces. Blobs live in the database rather than as files so that `:memory:` works and a run has one file of state. The method returns a concrete `LocalBlobSink`, which adds `get(name)` for tests and tools; `get` is not part of the `BlobSink` protocol.
- **`scheduler_auth(principal, audience)`:** `principal` is the expected bearer token; a token is accepted if it equals it, compared as UTF-8 bytes with `hmac.compare_digest` (which raises on a non-ASCII `str`). An unset principal refuses everything, as the protocol requires. `audience` is ignored: there is no token to carry one.

## Testing

- `LocalPlatform(":memory:")` for every test, inside `async with`.
- **One contract suite for `checkpoint_store` and `tick_lease` run against both backends.** Today `test_checkpoint.py`, `test_lease.py` and `test_platform_gcp.py` each hand-roll an incompatible Firestore fake, and only the lease one has compare-and-set. They are replaced by one async fake in `shared/tests` (modelled on Anthropic's `tests/fake_firestore.py`, which has `create`, `update_time` and `write_option`), and the suite is parametrized over `GcpPlatform` on that fake and `LocalPlatform`. Cases: first load empty, round trip, naive and non-UTC datetimes, separate documents and collections; lease taken, refused while held, taken after expiry (clock passed as `now`), taken after release, released by its owner, not released by a stranger, released on an exception.
- Blobs: put, replace, get, buckets are separate. Scheduler auth: accepted, wrong token, non-ASCII token, unset principal.
- Concurrency: `asyncio.gather` of `hold` on one lease on one platform admits exactly one; two `LocalPlatform`s on one file contend correctly and the loser reports not held; a statement that waits past the busy timeout raises.
- Location and lifecycle: a file path in a temporary directory whose parent does not exist yet; a second platform on the same file sees the first one's data; the connection starts once under concurrent first use; `aclose` twice is safe; use after `aclose` raises `RuntimeError`.
- Registry: `platforms.get("local", path=…)` resolves, and `test_get_names_the_known_platforms_for_an_unknown_one` now expects `known: ['gcp', 'local']`.

## Packaging

`shared/pyproject.toml` gets a `local = ["aiosqlite>=0.20"]` extra, next to `gcp`, `gcs` and `converse`, and `aiosqlite` goes into the dev dependency group so the shared tests can import it without an adapter pulling the extra in. `uv.lock` is regenerated in the same change.

## Risks

- **A platform that is never closed hangs interpreter exit** (non-daemon worker thread). Closing is part of the contract, and the tests and docs use the context manager.
- **One connection, one thread.** aiosqlite serializes statements on one worker thread, so a slow statement delays the others. That is fine for checkpoints, a lease and blobs; a future pending store with heavier queries is a reason to revisit it.
- **Two processes on one file.** WAL and the atomic upsert make the lease correct across processes; the busy timeout bounds the wait, and a longer wait raises.
- **Large blobs share the one thread** with everything else.
