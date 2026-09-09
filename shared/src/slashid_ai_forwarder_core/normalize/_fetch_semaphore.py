"""Global cap on concurrent external-object-store fetches per process.

Bedrock Converse S3 attachments, Bedrock mil_offload body-bytes, and
Vertex Gemini GCS attachments all acquire from this semaphore so a
fan-out tick (up to ``SLASHID_MAX_ROWS_PER_TICK=1000`` rows normalized
concurrently under ``asyncio.gather`` in the handler) can't burn
thousands of concurrent HEAD/GET requests against the object store's
connection pool.

Per-caller ``asyncio.Semaphore(N)`` instances don't provide meaningful
protection when the caller itself is fanned out: 1000 rows times 8
per-row slots = 8000 concurrent I/Os, well past aiohttp/urllib3
default pool sizes. A single process-global semaphore caps at N
regardless of how many rows the tick pulled in.

Lazy construction — ``asyncio.Semaphore`` binds to the running event
loop on first await, so we defer creation until first use to stay
compatible with test suites that spin up multiple loops (pytest-asyncio
``asyncio_mode = "auto"`` creates a fresh loop per test).
"""

from __future__ import annotations

from asyncio import Semaphore
from functools import cache

MAX_PARALLEL_FETCHES = 8


@cache
def get_fetch_semaphore() -> Semaphore:
    return Semaphore(MAX_PARALLEL_FETCHES)
