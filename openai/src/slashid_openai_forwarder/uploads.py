"""Files uploaded to ChatGPT, read afterwards from the Compliance Logs Platform.

ChatGPT itself has no hook, so an upload cannot be stopped; it can be
reported. Each ``CONVERSATION_MESSAGE`` log line is one message; a user
message that carries ``files`` becomes one invocation event naming them, so
``SensitiveFileSharedWithAI`` sees ChatGPT uploads as it sees Claude's.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    OpenAIIdentityDetails,
)
from slashid_ai_forwarder_core.platform import Checkpoint, CheckpointStore, load_or_start
from slashid_ai_forwarder_core.sink import push_invocations

from .compliance import ComplianceError, LogPage
from .config import Config

log = logging.getLogger(__name__)

PARSED_AS = "chatgpt-compliance-log"
# The model a user message names: none. A placeholder keeps the event valid,
# as the server requires a model id.
UPLOAD_MODEL = "chatgpt"


class LogSource(Protocol):
    """``ComplianceClient``'s half this reader uses."""

    async def log_files(self, *, after: datetime) -> LogPage: ...

    async def log_lines(self, file_id: str) -> list[bytes]: ...


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Actor(_Lenient):
    type: str | None = None
    user_id: str | None = None


class _File(_Lenient):
    id: str | None = None
    name: str | None = None


class _Author(_Lenient):
    type: str | None = None


class _Message(_Lenient):
    id: str
    created_at: datetime | None = None
    author: _Author | None = None
    files: list[_File] | None = None


class _Conversation(_Lenient):
    id: str


class LogLine(_Lenient):
    event_id: str
    type: str
    timestamp: datetime
    actor: _Actor | None = None
    message: _Message | None = None
    conversation: _Conversation | None = None


def upload_event(line: LogLine) -> AIInvocationObservedV1 | None:
    """The event for a user message with files; None for anything else.

    Only a user's own message: an assistant message's files are what the
    model produced, not what someone shared with it.
    """
    msg, actor, conv = line.message, line.actor, line.conversation
    if msg is None or conv is None or not msg.files:
        return None
    if msg.author is None or msg.author.type != "user":
        return None
    if actor is None or not actor.user_id:
        return None
    files = [AIAccessedFile(name=f.name, provenance="attachment") for f in msg.files if f.name]
    if not files:
        return None
    at = (msg.created_at or line.timestamp).isoformat(timespec="milliseconds")
    return AIInvocationObservedV1(
        request_id=f"chatgpt:{conv.id}:{msg.id}",
        timestamp=at.replace("+00:00", "Z"),
        identity_details=OpenAIIdentityDetails(user_id=actor.user_id),
        model=AIModel(id=UPLOAD_MODEL, provider="openai"),
        parsed_as=PARSED_AS,
        conversation_id=conv.id,
        accessed_files=files,
    )


def events_in(lines: list[bytes], seen: set[str]) -> list[AIInvocationObservedV1]:
    """A file's events. Lines are delivered at least once, so ``seen``
    (event ids, shared across the tick) drops a repeat."""
    out: list[AIInvocationObservedV1] = []
    for raw in lines:
        try:
            line = LogLine.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            log.warning("uploads: a log line did not parse; skipped")
            continue
        if line.event_id in seen:
            continue
        seen.add(line.event_id)
        if (event := upload_event(line)) is not None:
            out.append(event)
    return out


async def read_uploads(
    *,
    config: Config,
    compliance: LogSource,
    checkpoint: CheckpointStore,
    http: httpx.AsyncClient,
    now: datetime,
) -> dict[str, int]:
    """One tick: list log files after the watermark, push each file's
    events, and move the watermark past it.

    The watermark is the last pushed file's ``end_time`` and id, saved after
    every file, so a crash re-reads at most one file. The listing re-returns
    the file ``after`` names, which the saved id skips. A cold start is now:
    a backfill would report the whole retention window again.
    """
    start = await load_or_start(checkpoint, now=now)
    assert start.timestamp is not None
    after, last_id = start.timestamp, start.id
    files = pushed = 0
    seen: set[str] = set()
    while files < config.max_log_files_per_tick:
        try:
            page = await compliance.log_files(after=after)
        except ComplianceError as exc:
            log.warning("uploads: listing failed: %s", exc)
            break
        fresh = [f for f in page.data if f.id != last_id]
        for item in fresh[: config.max_log_files_per_tick - files]:
            try:
                lines = await compliance.log_lines(item.id)
            except ComplianceError as exc:
                # Held here: the next tick retries this file before any later one.
                log.warning("uploads: %s", exc)
                return {"log_files": files, "uploads": pushed}
            events = events_in(lines, seen)
            if events:
                pushed += await push_invocations(
                    http,
                    events,
                    endpoint=config.endpoint,
                    push_token=config.push_token,
                    max_retries=config.max_retries,
                )
            files += 1
            after = item.end_time or page.last_end_time or after
            last_id = item.id
            await checkpoint.save(Checkpoint(timestamp=after, id=last_id))
        if not page.has_more or not fresh:
            break
    return {"log_files": files, "uploads": pushed}
