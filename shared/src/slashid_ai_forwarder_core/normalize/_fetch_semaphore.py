"""Global cap on concurrent external-object-store fetches per process.

Bedrock Converse S3 attachments, Bedrock mil_offload body-bytes, and
Vertex Gemini GCS attachments all acquire from this semaphore so a
fan-out tick (up to ``SLASHID_MAX_ROWS_PER_TICK=1000`` rows normalized
concurrently under ``asyncio.gather`` in the handler) can't burn
thousands of concurrent HEAD/GET requests against the object store's
connection pool.

Per-caller ``asyncio.Semaphore(N)`` instances don't provide meaningful
protection when the caller itself is fanned out: 1000 rows times N
per-row slots = 1000 * N concurrent I/Os, well past aiohttp/urllib3
default pool sizes. A single process-global semaphore caps at N
regardless of how many rows the tick pulled in.

Cap sized against ``aiohttp.TCPConnector``'s default ``limit=100``
total connections — 50 leaves half the pool for other concurrent
outbound I/O (SlashID push via httpx uses a separate pool today, so
this is headroom for future additions rather than a binding
constraint). At this cap a 500-attachment tick runs its HEAD phase
in roughly ~500ms rather than the ~3s the earlier per-call-of-8
design would have produced.

Lazy construction — ``asyncio.Semaphore`` binds to the running event
loop on first await, so we defer creation until first use to stay
compatible with test suites that spin up multiple loops (pytest-asyncio
``asyncio_mode = "auto"`` creates a fresh loop per test).
"""

from __future__ import annotations

from asyncio import Semaphore
from functools import cache

MAX_PARALLEL_FETCHES = 50


@cache
def get_fetch_semaphore() -> Semaphore:
    return Semaphore(MAX_PARALLEL_FETCHES)
