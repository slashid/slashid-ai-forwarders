# LocalPlatform: the platform seam on SQLite

**Date:** 2026-10-01
**Status:** Design, ready for planning.
**Target repo:** `slashid-ai-forwarders`, `shared/` (`platform/local/`, `platform/__init__.py`, `pyproject.toml`).
**Follows:** the platform seam (#69) and the async checkpoint store (#77).

## Problem

The only platform is `gcp`, so nothing that uses the seam can run, or be tested, without Google. A local run (development, a Codex collector) wants checkpoints, a tick lease and blobs in a file; a test wants the same in memory.

## Goals

1. `LocalPlatform` implements `Platform` on SQLite: `checkpoint_store`, `tick_lease`, `blob_sink`, `scheduler_auth`.
2. It runs on a file or on `:memory:`. The caller chooses where: nothing here has a default location.
3. It exposes the database as a property, the way `GcpPlatform` exposes `firestore`, so an adapter with state of its own can share the connection.
4. A platform's lifecycle belongs to the code that opens it, not to the `Platform` protocol: `platforms.get` returns an async context manager, so a platform that holds a resource (a SQLite connection) is closed when the block ends, and one that holds none (`gcp`) simply yields.

## Non-goals

The Anthropic `PendingStore` on SQLite (on hold), `platform: "local"` in any adapter's config (Anthropic needs the pending store; Vertex's sources are GCP-specific), and replacing the Firestore fake in existing tests. The platform is built and tested on its own. The one change to existing services is how they obtain a platform, forced by the new `get` (see "Opening the platform in the services").

## Design

### The registry returns context managers

`platform/__init__.py` maps a name to a factory that is an async context manager, and `get` returns what the factory returns:

```python
_PLATFORMS = {
    "gcp": "slashid_ai_forwarder_core.platform.gcp:create_gcp_platform",
    "local": "slashid_ai_forwarder_core.platform.local:create_local_platform",
}

def get(name: str, **options: Any) -> AbstractAsyncContextManager[Platform]: ...
```

Use is `async with platforms.get("local", path=db_path) as platform:`. The `Platform` protocol itself is unchanged: no `aclose`, no `__aenter__`. Factories are imported only when asked for, so nothing loads `aiosqlite` or the Google libraries otherwise, and an unknown name still raises `ValueError` listing the known ones.

`create_gcp_platform(*, project, firestore_database)` is an `@asynccontextmanager` that yields `GcpPlatform(...)` and has nothing to clean up: its clients are built lazily and live as long as the process, as they do today. `GcpPlatform`'s constructor and the `firestore` property are unchanged.

### Opening a local platform

```python
@asynccontextmanager
async def create_local_platform(path: str | Path) -> AsyncIterator[LocalPlatform]:
    db = await open_database(path)
    try:
        yield LocalPlatform(db)
    finally:
        await db.close()
```

`path` is required: a file path, or `":memory:"`. Where a file belongs (a user data directory, a state directory, a temporary one) is the caller's decision, so nothing here reads the environment or imports a directory library. `open_database(path)`:

- for a file, creates the parent directory (`mkdir(parents=True, exist_ok=True, mode=0o700)`, where the mode applies only to the last component; SQLite cannot create it) and sets `journal_mode=WAL` and a busy timeout (SQLite's default is already five seconds, so set it only to name the value); for `:memory:` it does neither;
- connects with `isolation_level=None`, so every statement commits by itself and no implicit transaction is ever left open for a later one to trip over;
- creates the tables (`CREATE TABLE IF NOT EXISTS`).

`aiosqlite` starts a non-daemon worker thread per connection, so a connection that is never closed keeps the interpreter from exiting; that is why the factory closes it in a `finally`. After the block ends the platform's connection is closed and any use raises aiosqlite's own `ValueError: no active connection`; it is not wrapped.

### `LocalPlatform` and the database property

`LocalPlatform(db: aiosqlite.Connection)` takes an already-open connection and does no I/O of its own. Its `sqlite` property returns that connection, as `GcpPlatform.firestore` returns the Firestore client, so an adapter with state of its own shares the one connection. One connection serves every store: an in-memory database exists per connection, so sharing is what makes `:memory:` work. The connection is never used as a context manager by anyone (`async with connection` awaits it, which fails once started); its lifetime is the factory's.

### The interfaces

Times are stored as integer microseconds since the epoch (UTC), converted back with `EPOCH + timedelta(microseconds=us)`, which is exact. A timezone-aware `datetime` is converted to UTC and a naive one is taken as UTC, as `FirestoreCheckpointStore` does. Every operation below is a single statement, so none needs a transaction.

- **`checkpoint_store(collection, document)`:** table `checkpoints(collection, document, timestamp_us, id, PRIMARY KEY (collection, document))`. `load` returns `Checkpoint(None, None)` when the row is absent; `save` is `INSERT … ON CONFLICT DO UPDATE`.
- **`tick_lease(collection, document)`:** table `leases(collection, document, owner, expires_us, PRIMARY KEY (collection, document))`. `LocalTickLease` has the same public `take(lease, *, owner, now=None)` and `release(*, owner)` as `FirestoreTickLease`, and `hold(duration)` calls them, so the same tests can drive both. `now` is computed in Python and bound as a parameter, never SQLite's own clock. `take` is one atomic upsert, `INSERT … ON CONFLICT(collection, document) DO UPDATE SET owner = ?, expires_us = ? WHERE expires_us IS NULL OR expires_us <= ?`, and it holds the lease when `rowcount == 1`; a released lease has a NULL expiry, hence the `IS NULL` arm. `release` is `UPDATE … SET expires_us = NULL WHERE collection = ? AND document = ? AND owner = ?`, which does nothing, and raises nothing, when the lease has lapsed and been taken by someone else or the row is gone. `hold` yields `True` or `False` and releases on every way out of the block when it was held.
- **`blob_sink(bucket)`:** table `blobs(bucket, name, content_type, data, PRIMARY KEY (bucket, name))`; `put(name, data, *, content_type)` replaces. Blobs live in the database rather than as files so that `:memory:` works and a run has one file of state. The method returns a concrete `LocalBlobSink`, which adds `get(name)` for tests and tools; `get` is not part of the `BlobSink` protocol.
- **`scheduler_auth(principal, audience)`:** `principal` is the expected bearer token; a token is accepted if it equals it, compared as UTF-8 bytes with `hmac.compare_digest` (which raises on a non-ASCII `str`). An unset principal refuses everything, as the protocol requires. `audience` is ignored: there is no token to carry one.

### Opening the platform in the services

`platforms.get` is no longer a plain call, and the services cannot `await` inside the synchronous `app()` factory uvicorn runs. Anthropic's `build_backends(config)` and Vertex's `app()` therefore move the open into the app's lifespan: the lifespan does `async with platforms.get(config.platform, …) as platform:`, builds the stores, sources, lease and scheduler auth from it, and makes them available to the routes by rebinding the names the routes already read (`nonlocal` in the lifespan), then yields. `create_app` keeps taking its pieces as arguments, so tests keep injecting fakes, and gains a `backends` argument that the production `app()` points at the configured platform; a construction that gets its pieces up front still works, and giving neither (Vertex) is a `TypeError`. The http client Anthropic builds closes first on the way out, ahead of the platform. The routes, the readers and everything below them are unchanged.

## Testing

- `LocalPlatform(":memory:")` for every test, inside `async with`.
- **One contract suite for `checkpoint_store` and `tick_lease` run against both backends.** Today `test_checkpoint.py`, `test_lease.py` and `test_platform_gcp.py` each hand-roll an incompatible Firestore fake, and only the lease one has compare-and-set. They are replaced by one async fake in `shared/tests` (modelled on Anthropic's `tests/fake_firestore.py`, which has `create`, `update_time` and `write_option`), and the suite is parametrized over `GcpPlatform` on that fake and `LocalPlatform`. Cases: first load empty, round trip, naive and non-UTC datetimes, separate documents and collections; lease taken, refused while held, taken after expiry (clock passed as `now`), taken after release, released by its owner, not released by a stranger, released on an exception.
- Blobs: put, replace, get, buckets are separate. Scheduler auth: accepted, wrong token, non-ASCII token, unset principal.
- Concurrency: `asyncio.gather` of `hold` on one lease on one platform admits exactly one; two `LocalPlatform`s on one file contend correctly and the loser reports not held; a statement that waits past the busy timeout raises.
- Location and lifecycle: a file path in a temporary directory whose parent does not exist yet; a second platform on the same file sees the first one's data; the connection is closed when the block ends (use afterwards raises `ValueError`) and also when the block raises; `:memory:` platforms are independent of each other.
- `async with platforms.get("gcp", ...)` yields a `GcpPlatform` without touching Google, and leaving the block closes nothing.
- Registry: `platforms.get("local", path=…)` and `platforms.get("gcp", …)` return async context managers; a missing option is a `TypeError` at `get`; and `test_get_names_the_known_platforms_for_an_unknown_one` now expects `known: ['gcp', 'local']`. The existing tests that call `platform.get("gcp", ...)` and use the result directly are updated to `async with`.
- Services: the Anthropic and Vertex app tests keep injecting fakes into `create_app`; one test per service covers the production wiring, checking that the platform is opened when the app starts and closed when it stops (with a stub factory).

## Packaging

`shared/pyproject.toml` gets a `local = ["aiosqlite>=0.20"]` extra, next to `gcp`, `gcs` and `converse`, and `aiosqlite` goes into the dev dependency group so the shared tests can import it without an adapter pulling the extra in. `uv.lock` is regenerated in the same change.

## Risks

- **A connection that is never closed hangs interpreter exit** (non-daemon worker thread). The factory closes it in a `finally`, and `LocalPlatform` has no other way to be built.
- **One connection, one thread.** aiosqlite serializes statements on one worker thread, so a slow statement delays the others. That is fine for checkpoints, a lease and blobs; a future pending store with heavier queries is a reason to revisit it.
- **Two processes on one file.** WAL and the atomic upsert make the lease correct across processes; the busy timeout bounds the wait, and a longer wait raises.
- **Large blobs share the one thread** with everything else.
