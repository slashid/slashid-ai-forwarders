"""Wire models for the Compliance API — every shape the readers read.

The hook side has had a real ``PromptFrame`` since the beginning; this is
the same treatment for the other direction, so nothing below the client
handles a raw mapping. **Parsing happens once, at the client boundary**:
``ComplianceClient`` validates an envelope and hands the readers models,
which is why no reader reaches for a key that may not be there.

A **bad row is dropped, never raised**. One tick reads three feeds and
every transcript behind two of them; a single row the API spells in a
way these models do not expect would otherwise take down the whole pass,
including the records the other feeds already landed. ``_skip_bad_rows``
validates each row on its own and logs the ones it cannot, so the blast
radius of an unmodelled shape is one row.

Everything is ``extra="ignore"`` — the same ``_LenientModel`` the vendor
schemas use — because the API grows by addition and a new key must not
break a reader. Unknown **block** types are modelled explicitly rather
than tolerated implicitly: ``UnknownBlock`` catches them so a new block
type skips, exactly as the frame parser's union does.

Three envelopes, and the differences are not cosmetic:

* ``{data, has_more, first_id, last_id}`` — activities and chats, the
  two ordered feeds (:class:`CursorPage`).
* ``{data, next_page}`` — the local-session listing (:class:`TokenPage`).
* ``{data, next_page, session}`` — a session transcript, which carries
  the listing item it came from (:class:`SessionTranscript`). That item
  is where identity lives: a session **message** carries only ``type,
  id, role, created_at, provenance, model, content``.

A chat transcript is a fourth shape and not an envelope at all: the
endpoint answers the CHAT OBJECT, whose turns sit under
``chat_messages``. :class:`Chat` therefore models the listing row and the
transcript as one class — the listing row is that object without its
turns.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    JsonValue,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    field_validator,
)
from slashid_ai_forwarder_core.normalize._base import _LenientModel

log = logging.getLogger(__name__)


def _skip_bad_rows(value: Any, handler: ValidatorFunctionWrapHandler) -> list[Any]:
    """Validate a list row by row, dropping the rows that do not.

    ``handler`` validates the whole annotated ``list[T]``, so each row is
    offered as a one-element list. A row that fails is a log line: a tick
    that raised here would lose the feeds behind it and the records they
    had already completed.
    """
    if value is None:
        # Every one of these fields is nullable somewhere: ``files`` and
        # its two siblings on a chat message, and a ``data`` a feed may
        # yet spell as null rather than as an empty list.
        return []
    if not isinstance(value, list):
        return handler(value)
    out: list[Any] = []
    for row in value:
        try:
            out.extend(handler([row]))
        except ValidationError as exc:
            log.warning("compliance: dropping an unparseable row (%s)", exc.errors()[0]["msg"])
    return out


type Rows[T] = Annotated[list[T], WrapValidator(_skip_bad_rows)]


# --- content blocks -------------------------------------------------


class TextBlock(_LenientModel):
    """``truncated`` rides on every block: a transcript endpoint caps tool
    blocks and says so rather than lying about their length."""

    type: Literal["text"]
    text: str = ""
    truncated: bool = False
    # Chats only, and False throughout the corpus: a thinking block the
    # store kept the shape of and not the words.
    thinking_redacted: bool = False


class ToolUseBlock(_LenientModel):
    """``input`` is a JSON object in a session transcript and a JSON
    **string** in a chat one, so it stays ``JsonValue`` rather than
    pretending to a shape only one surface has."""

    type: Literal["tool_use"]
    id: str
    name: str
    input: JsonValue = None
    truncated: bool = False
    # MCP tool calls carry where they went; a built-in tool carries null.
    integration_name: str | None = None
    mcp_server_url: str | None = None


class ToolResultBlock(_LenientModel):
    """Where this sits is the whole difference between the two walks: in a
    CHAT it is inside the assistant message beside the ``tool_use`` that
    asked for it, in a SESSION transcript it is in the following user
    message."""

    type: Literal["tool_result"]
    tool_use_id: str
    name: str | None = None
    content: JsonValue = None
    is_error: bool = False
    truncated: bool = False
    integration_name: str | None = None
    mcp_server_url: str | None = None


class UnknownBlock(_LenientModel):
    """Any block type not modelled above — skipped downstream, never
    rejected, which is the rule the frame parser follows too."""

    type: str


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock | UnknownBlock
# Bare union, like the vendor schema's: ``UnknownBlock.type`` is a plain
# str and pydantic wants a Literal to discriminate on. Smart-union still
# prefers the specific variant whose Literal matches.


# --- the pieces a listing item carries ------------------------------


class UserRef(_LenientModel):
    """The listing's ``user``. Its ``id`` is byte-identical to a frame's
    ``actor.id``, which is what keeps a reader-emitted event and a
    hook-emitted one on one graph identity."""

    id: str | None = None
    email_address: str | None = None


class Provenance(_LenientModel):
    """An object — ``{"type": "client_asserted"}`` — never a bare string.

    ``client_asserted``, ``synthetic_marker`` and ``content_unavailable``,
    the last carrying a ``reason``. ``type`` is an open string because the
    schema says to tolerate unrecognized values, and a turn we cannot
    classify is not one to emit.
    """

    type: str
    reason: str | None = None


class FileEntry(_LenientModel):
    """One ``files[]`` entry. The only cheap metadata there is: ``HEAD``
    on the content endpoint 404s on every attachment, so ``size_bytes``
    and ``md5`` are what a fetch decision and a digest rest on."""

    id: str
    filename: str | None = None
    # Sometimes a bare extension ("txt"), which is not a media type;
    # `parse_media_type` degrades that to None rather than raising.
    mime_type: str | None = None
    size_bytes: int | None = None
    md5: str | None = None
    created_at: str | None = None

    @field_validator("md5")
    @classmethod
    def _lowercase(cls, value: str | None) -> str | None:
        """Lowercase hex on the wire; normalized so a comparison against a
        recomputed digest cannot fail on case."""
        return value.lower() if value else None


class ArtifactRef(_LenientModel):
    """The assistant's own output, modelled so it is visibly *not* read as
    ingress: an artifact belongs nowhere near ``accessed_files``."""

    id: str | None = None
    version_id: str | None = None
    title: str | None = None
    artifact_type: str | None = None


# --- messages -------------------------------------------------------


class TranscriptMessage(_LenientModel):
    """What the two transcripts share: a role, a content list and a clock.

    ``role`` is an open string rather than a Literal — a role invented
    next quarter must reach the walk and be skipped there, not fail the
    page it arrived on.
    """

    id: str | None = None
    role: str | None = None
    created_at: str | None = None
    content: Rows[ContentBlock] = Field(default_factory=list)

    @property
    def at(self) -> datetime | None:
        """``created_at`` parsed, or None. The feeds spell the same instant
        differently — an activity carries an offset, a message a ``Z`` —
        so a caller comparing the strings compares spellings."""
        return _parse(self.created_at)


class SessionMessage(TranscriptMessage):
    """A local-session turn, and the whole of one: ``type, id, role,
    created_at, provenance, model, content``.

    No user and no files. Identity comes from the listing item, and a
    reader that looked for it here would drop every standalone event.
    """

    type: str | None = None
    provenance: Provenance | None = None
    model: str | None = None


class ChatMessage(TranscriptMessage):
    """A claude.ai turn. No ``model`` and no ``provenance`` — the chat
    object carries the one and the store is canonical, so there is no
    replayed history to mark."""

    files: Rows[FileEntry] = Field(default_factory=list)
    generated_files: Rows[FileEntry] = Field(default_factory=list)
    artifacts: Rows[ArtifactRef] = Field(default_factory=list)


# --- listing items --------------------------------------------------


class SessionListing(_LenientModel):
    """One ``/apps/sessions/local`` row, and a session transcript's
    ``session``. The ``clls_`` id decodes to the frame's ``session_id``."""

    type: str | None = None
    id: str = ""
    organization_uuid: str | None = None
    workspace_id: str | None = None
    user: UserRef | None = None
    product_surface: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    truncated: bool = False


class Chat(_LenientModel):
    """A claude.ai conversation: one class for the listing row and the
    transcript, because the transcript is that row plus ``chat_messages``.

    ``model`` is the only one there is — no chat message repeats it — and
    ``href`` is the only place the uuid a frame calls ``session_id``
    appears. The ``claude_chat_…`` id is carried by no frame.
    """

    id: str = ""
    name: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    deleted_at: str | None = None
    organization_id: str | None = None
    organization_uuid: str | None = None
    project_id: str | None = None
    model: str | None = None
    user: UserRef | None = None
    href: str | None = None
    chat_messages: Rows[ChatMessage] = Field(default_factory=list)


class Actor(_LenientModel):
    """Who the activity feed says acted. ``user_agent`` is the real client
    string (``claude-cli/2.1.278``) that no frame carries."""

    type: str | None = None
    user_id: str | None = None
    email_address: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None


class Activity(_LenientModel):
    """One audit row. Ten types were recorded and only one is a denial, so
    everything past ``type`` is optional: the rows the reader filters out
    carry entirely different fields, and each still has to parse."""

    id: str | None = None
    type: str = ""
    created_at: str | None = None
    organization_id: str | None = None
    organization_uuid: str | None = None
    actor: Actor = Field(default_factory=Actor)
    # Only a denial carries these three.
    request_id: str | None = None
    conversation_id: str | None = None
    surface: str | None = None

    @property
    def at(self) -> datetime | None:
        return _parse(self.created_at)


# --- envelopes ------------------------------------------------------


class CursorPage[T](_LenientModel):
    """``{data, has_more, first_id, last_id}`` — the two ordered feeds.

    The page token the next request sends as ``after_id`` is this body's
    ``last_id``; the names differ and pinning both is the point.
    """

    data: Rows[T] = Field(default_factory=list)
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class TokenPage[T](_LenientModel):
    """``{data, next_page}`` — the unorderable local-session listing."""

    data: Rows[T] = Field(default_factory=list)
    next_page: str | None = None


class SessionTranscript(TokenPage[SessionMessage]):
    """``{data, next_page, session}``. The ``session`` is the listing item,
    which is the only half of this response carrying an identity."""

    session: SessionListing | None = None


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None
