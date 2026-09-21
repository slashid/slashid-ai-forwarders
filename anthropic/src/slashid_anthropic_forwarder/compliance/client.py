"""The Compliance API — three feeds that do not share a query vocabulary.

Base ``https://api.anthropic.com/v1/compliance``; every request carries
``x-api-key`` and ``anthropic-version: 2023-06-01``.

| Feed           | Lower bound        | Ordering                | Page token   |
| -------------- | ------------------ | ----------------------- | ------------ |
| activities     | ``created_at.gte`` | ``order=asc``           | ``last_id``  |
| chats          | ``updated_at.gte`` | ``order_by=updated_at`` | ``last_id``  |
| local sessions | ``updated_at.gte`` | none exists             | ``next_page``|

Measured against the live API, and none of the three differences is
cosmetic:

* activities default to ``desc``. Resuming from a saved watermark
  without ``order=asc`` walks steadily further into the past and never
  sees a new denial — 200s, full pages, zero new rows, no error.
* chats **reject** ``updated_at.gte`` unless ``order_by=updated_at``
  rides with it, and the parameter is ``order_by``, not ``order``.
* local sessions accept neither ``order`` nor ``order_by`` — both are
  rejected — and answer newest-first. There is no forward stream, so a
  reader drains the whole lagging window each tick and the drain
  reports whether it finished.

Three more facts the paths and the filters rest on. The local session
listing is ``/apps/sessions/local``, **not** ``/apps/local_sessions``.
The bound is dotted — ``created_at[gte]`` is rejected. And
``organization_uuid`` is not a query parameter on either listing, so
filtering to the bound organization happens in Python, over the rows a
listing returns.

The envelopes differ too: the two ordered feeds answer ``{data,
has_more, first_id, last_id}``, the session listing and a session
transcript answer ``{data, next_page, …}``, and a **chat** transcript
answers the chat object itself, whose turns sit under ``chat_messages``
and whose ``model`` no message repeats.

This client does not own its ``httpx.AsyncClient``: the tick shares one
with the push path, and every header here is per request.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.anthropic.com/v1/compliance"
API_VERSION = "2023-06-01"

_ACTIVITIES = "/activities"
_CHATS = "/apps/chats"
# Not ``/apps/local_sessions``: that 404s. The noun order is the other
# way round, and it is the one path here worth pinning in a constant.
_SESSIONS = "/apps/sessions/local"
_FILE_CONTENT = "/apps/chats/files/{file_id}/content"
# Transcript pages, which a listing's limit does not bound.
_MESSAGE_PAGE = 1_000

# Transcript endpoints cap each tool block at this many bytes and flag the
# block ``truncated``; ``-1`` asks for the whole block (~1 MiB ceiling).
TOOL_BLOCK_DEFAULT_BYTES = 10_000
TOOL_BLOCK_FULL = -1

DENIED_ACTIVITY = "inference_hooks_request_denied"
# Our own reads are audited as this, so a poller adds noise to the
# tenant's audit record and Reader A filters the feed by type.
OWN_READ_ACTIVITY = "compliance_api_accessed"

# ``provenance`` is an object — {"type": "client_asserted"} — never a bare
# string, and a fourth value exists: ``content_unavailable``, carrying a
# ``reason`` of not_captured, client_aborted, cmek_key_revoked,
# retention_elapsed or oversize. A produced turn has no provenance at all.
REPLAYED = "client_asserted"
SYNTHETIC = "synthetic_marker"
UNAVAILABLE = "content_unavailable"
NOT_PRODUCED = frozenset({REPLAYED, SYNTHETIC, UNAVAILABLE})


class ComplianceError(Exception):
    """A feed answered something other than 2xx, or unparseable JSON."""


@dataclass(frozen=True)
class Drain:
    """A pass over the unorderable local-sessions listing.

    ``complete`` is False when the cap cut the listing. The listing is
    newest-first, so the untouched tail is the *oldest* — advancing the
    watermark past it would lose those sessions permanently, hardest on
    the busiest tenants and immediately after an outage.
    """

    sessions: list[dict[str, Any]]
    complete: bool


def provenance_type(message: Mapping[str, Any]) -> str | None:
    """The ``type`` inside a message's ``provenance`` object, or None.

    ``None`` is the common answer and the meaningful one: in the recorded
    transcripts a newly-produced turn carries ``"provenance": null`` and a
    replayed or synthetic one carries an object.
    """
    provenance = message.get("provenance")
    if isinstance(provenance, Mapping):
        kind = provenance.get("type")
        return kind if isinstance(kind, str) else None
    return None


def decode_session_id(session_id: str) -> str | None:
    """The frame's ``session_id`` out of a ``clls_`` identifier.

    ``clls_`` is URL-safe base64 of ``{"v":1,"o":<org>,"p":<account>,
    "s":<session>}``. Decoding the listing is the documented way across:
    constructing the id works but leans on a versioned encoding and on
    an account uuid no frame carries. A shape we cannot decode is a miss,
    never an exception — the encoding is allowed to change under us.
    """
    if not session_id.startswith("clls_"):
        return None
    raw = session_id.removeprefix("clls_")
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        log.warning("compliance: undecodable session id (encoding changed?)")
        return None
    session = payload.get("s") if isinstance(payload, Mapping) else None
    return session if isinstance(session, str) else None


def chat_session_id(chat: Mapping[str, Any]) -> str | None:
    """The frame's ``session_id`` for a chat: the uuid its ``href`` ends with.

    Measured on every claude.ai conversation in the tenant, three of
    three. It is **not** the ``claude_chat_…`` id, which no frame
    carries, so a reader that used that id would file one conversation
    under two identifiers depending on which source saw it.
    """
    href = chat.get("href")
    if not isinstance(href, str) or "/" not in href:
        return None
    return href.rstrip("/").rsplit("/", 1)[-1] or None


def created_at(row: Mapping[str, Any]) -> datetime | None:
    """A row's ``created_at``, parsed. ``None`` on anything unparseable.

    One parser for both feeds, because they spell the same instant
    differently — an activity carries an offset, a message a ``Z`` — and
    a caller comparing the strings would be comparing spellings.
    """
    raw = row.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class ComplianceClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str = API_BASE,
    ) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}

    async def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._base}{path}", params=dict(params), headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise ComplianceError(f"{path}: {exc!r}") from exc
        if response.status_code // 100 != 2:
            raise ComplianceError(f"{path}: HTTP {response.status_code} {response.text[:200]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ComplianceError(f"{path}: unparseable body") from exc
        return payload if isinstance(payload, dict) else {"data": payload}

    # --- feed one: activities, ordered, resumable ---------------------

    async def iter_activities(
        self, *, since: datetime, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """Oldest-first from ``since``. ``order=asc`` is mandatory."""
        params: dict[str, Any] = {
            "created_at.gte": since.isoformat(),
            "order": "asc",
            "limit": page_size,
        }
        while True:
            payload = await self._get(_ACTIVITIES, params)
            rows = _rows(payload)
            for row in rows:
                yield row
            if not payload.get("has_more") or not payload.get("last_id") or not rows:
                return
            params = {**params, "after_id": payload["last_id"]}

    # --- feed two: chats, ordered only when asked ---------------------

    async def iter_chats(
        self, *, since: datetime, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        """``order_by`` is not optional here: the bound is rejected without it."""
        params: dict[str, Any] = {
            "updated_at.gte": since.isoformat(),
            "order_by": "updated_at",
            "limit": page_size,
        }
        while True:
            payload = await self._get(_CHATS, params)
            rows = _rows(payload)
            for row in rows:
                yield row
            if not payload.get("has_more") or not payload.get("last_id") or not rows:
                return
            params = {**params, "after_id": payload["last_id"]}

    # --- feed three: local sessions, unorderable ----------------------

    async def drain_local_sessions(self, *, since: datetime, limit: int) -> Drain:
        """Drain the lagging window. No ordering parameter exists, so this
        cannot stream forward from a watermark; it reads the window and
        leans on the pending store's tombstones to suppress repeats.

        ``limit`` bounds *sessions*, not pages: a tick that hits it leaves
        the oldest sessions unread and the returned ``complete`` is False.
        """
        # No `organization_uuid` here: both listings reject it as a query
        # parameter, so that filter is the caller's and runs over these
        # rows.
        params: dict[str, Any] = {"updated_at.gte": since.isoformat(), "limit": min(limit, 100)}
        sessions: list[dict[str, Any]] = []
        while True:
            payload = await self._get(_SESSIONS, params)
            rows = _rows(payload)
            for row in rows:
                if len(sessions) >= limit:
                    log.warning(
                        "compliance: local-session drain cut at %d; oldest sessions unread, "
                        "watermark stays put",
                        limit,
                    )
                    return Drain(sessions=sessions, complete=False)
                sessions.append(row)
            token = payload.get("next_page")
            if not token or not rows:
                return Drain(sessions=sessions, complete=True)
            params = {**params, "page": token}

    # --- transcripts and bytes ----------------------------------------

    async def chat(
        self, chat_id: str, *, tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES
    ) -> dict[str, Any]:
        """The chat object, turns included. Its ``model`` is the only one
        there is — no chat message carries one."""
        return await self._get(f"{_CHATS}/{chat_id}/messages", _tool_caps(tool_block_bytes))

    async def chat_messages(
        self, chat_id: str, *, tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES
    ) -> list[dict[str, Any]]:
        """A chat's turns, which sit under ``chat_messages`` rather than
        ``data``: reading ``data`` here yields nothing and says nothing
        about why."""
        return _chat_messages(await self.chat(chat_id, tool_block_bytes=tool_block_bytes))

    async def session_messages(
        self, session_id: str, *, tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES
    ) -> list[dict[str, Any]]:
        """A local-session transcript, ``{session, data, next_page}``.

        It paginates like the listing it came from rather than like the
        two ordered feeds, so this follows ``next_page`` to the end: a
        partial transcript silently hides produced turns.
        """
        params: dict[str, Any] = {**_tool_caps(tool_block_bytes), "limit": _MESSAGE_PAGE}
        messages: list[dict[str, Any]] = []
        while True:
            payload = await self._get(f"{_SESSIONS}/{session_id}/messages", params)
            rows = _rows(payload)
            messages.extend(rows)
            token = payload.get("next_page")
            if not token or not rows:
                return messages
            params = {**params, "page": token}

    async def file_content(self, file_id: str) -> bytes:
        """The whole stored body. There is no ``HEAD`` to size it first —
        measured: ``HEAD`` 404s on every attachment — so the listing's
        ``size_bytes`` is what decides whether this is called at all."""
        url = f"{self._base}{_FILE_CONTENT.format(file_id=file_id)}"
        try:
            response = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ComplianceError(f"file {file_id}: {exc!r}") from exc
        if response.status_code // 100 != 2:
            raise ComplianceError(f"file {file_id}: HTTP {response.status_code}")
        return response.content


def _tool_caps(tool_block_bytes: int) -> dict[str, Any]:
    """Both caps move together; ``-1`` lifts them to the ~1 MiB ceiling."""
    return {
        "tool_result_max_bytes": tool_block_bytes,
        "tool_use_input_max_bytes": tool_block_bytes,
    }


def _rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, Sequence):
        return []
    return [row for row in data if isinstance(row, dict)]


def _chat_messages(chat: Mapping[str, Any]) -> list[dict[str, Any]]:
    turns = chat.get("chat_messages")
    if not isinstance(turns, Sequence):
        return []
    return [turn for turn in turns if isinstance(turn, dict)]
