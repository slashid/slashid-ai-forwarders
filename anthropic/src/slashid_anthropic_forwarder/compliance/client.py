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

Every response is parsed here and nowhere else: a reader receives
``compliance.schema`` models, never a mapping, and a row that will not
parse is dropped with a log line rather than taking the tick down.

This client does not own its ``httpx.AsyncClient``: the tick shares one
with the push path, and every header here is per request.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from .schema import (
    Activity,
    Chat,
    ChatMessage,
    CursorPage,
    SessionListing,
    SessionMessage,
    SessionTranscript,
    TokenPage,
)

log = logging.getLogger(__name__)

API_BASE = "https://api.anthropic.com/v1/compliance"
API_VERSION = "2023-06-01"

_ACTIVITIES = "/activities"
_CHATS = "/apps/chats"
# Not ``/apps/local_sessions``: that 404s. The noun order is the other
# way round, and it is the one path here worth pinning in a constant.
_SESSIONS = "/apps/sessions/local"
_FILE_CONTENT = "/apps/chats/files/{file_id}/content"
# Transcript pages, which a listing's limit does not bound. Small, because
# one page of a long session at 1,000 messages regularly outlived a 10s
# read timeout, and a smaller page costs round trips, not correctness.
_MESSAGE_PAGE = 200
# Chat transcripts page separately; the endpoint accepts up to this many.
_CHAT_PAGE = 1_000

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

    sessions: list[SessionListing]
    complete: bool


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


def chat_session_id(chat: Chat) -> str | None:
    """The frame's ``session_id`` for a chat: the uuid its ``href`` ends with.

    Measured on every claude.ai conversation in the tenant, three of
    three. It is **not** the ``claude_chat_…`` id, which no frame
    carries, so a reader that used that id would file one conversation
    under two identifiers depending on which source saw it.
    """
    href = chat.href
    if not href or "/" not in href:
        return None
    return href.rstrip("/").rsplit("/", 1)[-1] or None


class ComplianceClient:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str = API_BASE,
        timeout: float | None = None,
    ) -> None:
        """``timeout`` bounds each request, overriding the shared client's
        own, which is tuned for the hook rather than for transcript pages."""
        self._client = client
        self._timeout = httpx.USE_CLIENT_DEFAULT if timeout is None else timeout
        self._base = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key, "anthropic-version": API_VERSION}

    async def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """The one mapping in this package: every caller validates it into
        a ``schema`` model on the next line."""
        try:
            response = await self._client.get(
                f"{self._base}{path}",
                params=dict(params),
                headers=self._headers,
                timeout=self._timeout,
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
    ) -> AsyncIterator[Activity]:
        """Oldest-first from ``since``. ``order=asc`` is mandatory."""
        params: dict[str, Any] = {
            "created_at.gte": since.isoformat(),
            "order": "asc",
            "limit": page_size,
        }
        while True:
            page = CursorPage[Activity].model_validate(await self._get(_ACTIVITIES, params))
            for row in page.data:
                yield row
            if not page.has_more or not page.last_id or not page.data:
                return
            params = {**params, "after_id": page.last_id}

    # --- feed two: chats, ordered only when asked ---------------------

    async def iter_chats(self, *, since: datetime, page_size: int = 100) -> AsyncIterator[Chat]:
        """``order_by`` is not optional here: the bound is rejected without it."""
        params: dict[str, Any] = {
            "updated_at.gte": since.isoformat(),
            "order_by": "updated_at",
            "limit": page_size,
        }
        while True:
            page = CursorPage[Chat].model_validate(await self._get(_CHATS, params))
            for row in page.data:
                yield row
            if not page.has_more or not page.last_id or not page.data:
                return
            params = {**params, "after_id": page.last_id}

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
        sessions: list[SessionListing] = []
        while True:
            page = TokenPage[SessionListing].model_validate(await self._get(_SESSIONS, params))
            for row in page.data:
                if len(sessions) >= limit:
                    log.warning(
                        "compliance: local-session drain cut at %d; oldest sessions unread, "
                        "watermark stays put",
                        limit,
                    )
                    return Drain(sessions=sessions, complete=False)
                sessions.append(row)
            if not page.next_page or not page.data:
                return Drain(sessions=sessions, complete=True)
            params = {**params, "page": page.next_page}

    # --- transcripts and bytes ----------------------------------------

    async def chat(self, chat_id: str) -> Chat:
        """The chat object with every turn. Its ``model`` is the only one
        there is — no chat message carries one.

        Unlike a session transcript, this endpoint takes no tool-block caps
        (it answers 400 to them) and pages with ``after_id``: the id of the
        previous page's last message. The response's own ``last_id`` is not
        that id, so it is not used.
        """
        params: dict[str, Any] = {"limit": _CHAT_PAGE}
        first: Chat | None = None
        messages: list[ChatMessage] = []
        while True:
            page = Chat.model_validate(await self._get(f"{_CHATS}/{chat_id}/messages", params))
            first = first or page
            messages.extend(page.chat_messages)
            last = page.chat_messages[-1].id if page.chat_messages else None
            if not page.has_more or not last:
                return first.model_copy(update={"chat_messages": messages, "has_more": False})
            params = {**params, "after_id": last}

    async def chat_messages(self, chat_id: str) -> list[ChatMessage]:
        """A chat's turns, which sit under ``chat_messages`` rather than
        ``data``: reading ``data`` here yields nothing and says nothing
        about why."""
        chat = await self.chat(chat_id)
        return chat.chat_messages

    async def session_messages(
        self, session_id: str, *, tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES
    ) -> list[SessionMessage]:
        """A local-session transcript, ``{session, data, next_page}``.

        It paginates like the listing it came from rather than like the
        two ordered feeds, so this follows ``next_page`` to the end: a
        partial transcript silently hides produced turns.
        """
        params: dict[str, Any] = {**_tool_caps(tool_block_bytes), "limit": _MESSAGE_PAGE}
        messages: list[SessionMessage] = []
        while True:
            payload = await self._get(f"{_SESSIONS}/{session_id}/messages", params)
            page = SessionTranscript.model_validate(payload)
            messages.extend(page.data)
            if not page.next_page or not page.data:
                return messages
            params = {**params, "page": page.next_page}

    async def session_tail(
        self,
        session_id: str,
        *,
        horizon: datetime,
        tool_block_bytes: int = TOOL_BLOCK_DEFAULT_BYTES,
    ) -> tuple[list[SessionMessage], bool]:
        """The end of a transcript that holds every turn from ``horizon`` on,
        oldest-first, and whether that is the whole transcript.

        Read newest-first (``order=desc``) and stopped at the first
        assistant message older than ``horizon``, which is kept: a turn's
        round starts after the assistant message before it, so every turn
        from the horizon on has its round in the tail. A turn cut off by
        where reading stopped began before the horizon and is skipped.
        """
        params: dict[str, Any] = {
            **_tool_caps(tool_block_bytes),
            "limit": _MESSAGE_PAGE,
            "order": "desc",
        }
        newest_first: list[SessionMessage] = []
        while True:
            payload = await self._get(f"{_SESSIONS}/{session_id}/messages", params)
            page = SessionTranscript.model_validate(payload)
            start = len(newest_first)
            newest_first.extend(page.data)
            for i in range(start, len(newest_first)):
                message = newest_first[i]
                if message.role == "assistant" and _before(message.created_at, horizon):
                    return newest_first[i::-1], False
            if not page.next_page or not page.data:
                return newest_first[::-1], True
            params = {**params, "page": page.next_page}

    async def file_content(self, file_id: str) -> bytes:
        """The whole stored body. There is no ``HEAD`` to size it first —
        measured: ``HEAD`` 404s on every attachment — so the listing's
        ``size_bytes`` is what decides whether this is called at all."""
        url = f"{self._base}{_FILE_CONTENT.format(file_id=file_id)}"
        try:
            response = await self._client.get(url, headers=self._headers, timeout=self._timeout)
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


def _before(created_at: str | None, horizon: datetime) -> bool:
    """Whether a message's clock is older than ``horizon``. A missing or
    unreadable clock is not, so reading goes on past it."""
    if not created_at:
        return False
    try:
        return datetime.fromisoformat(created_at) < horizon
    except ValueError:
        return False
