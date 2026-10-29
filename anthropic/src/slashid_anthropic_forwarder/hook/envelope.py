"""Frame → the partial ``AIInvocationObservedV1`` a pending record holds.

Attribution runs one round behind. Frame N carries ``[… U(n-1), A(n-1),
U(n)]`` and the record names ``A(n-1)``, so ``input`` is the transcript
truncated before that run and ``used_tools`` / ``accessed_files`` are
the round it consumed, ``U(n-1)``. The fresh round ``U(n)`` drives the
verdict and belongs to the tail record; it is not part of this one.

Nothing here decides which round that is. Every function scans the last
round of the messages it is handed, so the caller chooses the boundary
by choosing the slice. ``output`` and ``stop_reason`` are the trailing
run's, merged into one response by ``partial_event``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath

from slashid_ai_forwarder_core.content_utils import truncate_middle
from slashid_ai_forwarder_core.events import (
    AIAccessedFile,
    AIInvocationObservedV1,
    AIModel,
    AnthropicIdentityDetails,
    EventEnvelope,
    build_event_from_normalized,
)
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    message_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicContentBlock,
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicRequestMessage,
    AnthropicTextBlock,
    AnthropicThinkingBlock,
    AnthropicToolUseBlock,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.normalized.media_types import parse_media_type
from slashid_ai_forwarder_core.normalize.normalized.tools import build_tools_declared
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant

from ..config import Config
from .frame import PromptFrame, split_transcript

PARSED_AS = "anthropic-inference-hook"

# claude.ai lists uploads as ``<file_path>/mnt/user-data/uploads/<name></file_path>``
# inside a text block ahead of the attachment blocks. The listed order does
# not match the block order, so a nameless block is paired by media type.
_UPLOAD_PATH = re.compile(r"<file_path>(.*?)</file_path>")


def _uploaded_names(messages: list[AnthropicRequestMessage]) -> list[str]:
    names: list[str] = []
    for msg in messages:
        for block in msg.content:
            if isinstance(block, AnthropicTextBlock) and "<uploaded_files>" in block.text:
                names.extend(p.rsplit("/", 1)[-1] for p in _UPLOAD_PATH.findall(block.text))
    return names


# The sandbox path of an upload replaces spaces (and other punctuation) in
# its name with ``_``. Folding both sides the same way pairs the user's own
# name with its listing; the folded form is never sent — the server
# normalises names for matching on its own.
_UNSAFE = re.compile(r"[^0-9A-Za-z.]+")


def _fold(name: str) -> str:
    return _UNSAFE.sub("_", PurePosixPath(name).name)


def _name_for(block: AnthropicAttachmentBlock, candidates: list[str]) -> str | None:
    """Consume a listed name for this block. Consuming matters: two uploads
    of the same media type must not both claim the first listed name, and a
    block that brings its own name consumes its (underscored) listing."""
    if block.file_name:
        folded = _fold(block.file_name)
        for i, name in enumerate(candidates):
            if name == block.file_name or _fold(name) == folded:
                del candidates[i]
                break
        return block.file_name
    for i, name in enumerate(candidates):
        guessed, _ = mimetypes.guess_type(name)
        if guessed == block.media_type:
            return candidates.pop(i)
    return None


def attachment_files(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> list[AIAccessedFile]:
    """One entry per attachment in the last round of ``messages``.

    Which round that is belongs to the caller: the whole frame scans the
    fresh round, ``split.before`` scans the round the previous run
    consumed.

    A hook attachment carries extracted text and never bytes, so the
    digests are over that text. ``byte_length`` is the upload's
    ``size_bytes`` when the frame has one: it is the size of the file the
    user sent, the one a server-side name match compares against the
    crawled file; the text's length only when there is no size.

    An attachment with no text (an image, or a binary upload claude.ai
    keeps in its sandbox) is still a file the user put in front of the
    model: it is named and sized, with nothing to hash. One that nothing
    names is not an entry — there is nothing to match it on.
    """
    last_round = list(after_last_assistant(messages))
    candidates = _uploaded_names(last_round)
    out: list[AIAccessedFile] = []
    for msg in last_round:
        for block in msg.content:
            if not isinstance(block, AnthropicAttachmentBlock):
                continue
            name = _name_for(block, candidates)
            if block.text is None:
                if name is not None:
                    out.append(
                        AIAccessedFile(
                            name=name,
                            media_type=parse_media_type(block.media_type),
                            byte_length=block.size_bytes,
                            provenance="attachment",
                        )
                    )
                continue
            data = block.text.encode()
            out.append(
                AIAccessedFile(
                    name=name,
                    content_hashes={
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "sha1": hashlib.sha1(data).hexdigest(),
                        "md5": hashlib.md5(data).hexdigest(),
                    },
                    media_type=parse_media_type(block.media_type),
                    byte_length=block.size_bytes if block.size_bytes is not None else len(data),
                    redacted_content=(
                        truncate_middle(block.text, config.max_content_size)
                        if config.include_raw_content
                        else None
                    ),
                    provenance="attachment",
                )
            )
    return out


def _uploads_by_folded_name(
    messages: list[AnthropicRequestMessage],
) -> dict[str, AnthropicAttachmentBlock]:
    """Every upload in the transcript that brought its own name, keyed by the
    folded form its sandbox path takes. Every round counts: Claude reads an
    upload with a sandbox tool rounds after the user sent it."""
    out: dict[str, AnthropicAttachmentBlock] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, AnthropicAttachmentBlock) and block.file_name:
                out.setdefault(_fold(block.file_name), block)
    return out


def _as_uploaded(
    files: list[AIAccessedFile], messages: list[AnthropicRequestMessage]
) -> list[AIAccessedFile]:
    """A sandbox read names an upload by its sandbox path; the upload's own
    attachment gives it back the name the user gave it and its size."""
    uploads = _uploads_by_folded_name(messages)
    if not uploads:
        return files
    out: list[AIAccessedFile] = []
    for f in files:
        upload = uploads.get(_fold(f.name)) if f.provenance == "tool_result" and f.name else None
        if upload is None:
            out.append(f)
            continue
        out.append(
            f.model_copy(
                update={
                    "name": upload.file_name,
                    "byte_length": (
                        upload.size_bytes if upload.size_bytes is not None else f.byte_length
                    ),
                }
            )
        )
    return out


async def _normalized(
    messages: list[AnthropicRequestMessage],
    *,
    answer: AnthropicMessage | None = None,
    config: Config,
) -> NormalizedInvocation:
    """Canonicalize the messages it is given, with an empty response.

    Empty on the verdict's path, which is judging a round nothing has
    answered; ``partial_event`` passes the trailing run. ``finalize``
    then adds the last round's tool-result files behind the attachment
    entries and dedupes the union, first-seen wins.
    """
    normalized = await message_to_normalized_invocation(
        AnthropicRequestBody(messages=messages),
        answer or AnthropicMessage(type="message", role="assistant", content=[]),
        config=config,
    )
    normalized.accessed_files = attachment_files(messages, config=config)
    finalized = finalize(normalized, config=config)
    finalized.accessed_files = _as_uploaded(finalized.accessed_files, messages)
    return finalized


async def accessed_files_for(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> list[AIAccessedFile]:
    """Files the last round of ``messages`` puts in front of the model.

    The verdict passes the whole frame and checks the fresh round before
    answering; the record passes ``split.before`` and reports the round
    the previous run consumed. One recipe either way, so the file a
    verdict allowed cannot be recorded under a different digest.
    """
    return (await _normalized(messages, config=config)).accessed_files


def signed_at_iso(signed_at: int) -> str:
    """The attested ``webhook-timestamp`` as the wire's timestamp."""
    return datetime.fromtimestamp(signed_at, tz=UTC).isoformat()


def _tool_names(messages: list[AnthropicRequestMessage]) -> list[str]:
    """Distinct raw tool names, first-seen order. Distinct matters:
    ``build_tools_declared`` dedupes servers but not tools."""
    seen: dict[str, None] = {}
    for msg in messages:
        for block in msg.content:
            if isinstance(block, AnthropicToolUseBlock):
                seen.setdefault(block.name)
    return list(seen)


def _answer(run: list[AnthropicRequestMessage]) -> AnthropicMessage:
    """The trailing assistant run as the one response it is.

    A run can arrive as several assistant messages — one answer in
    pieces, not several answers — so the blocks concatenate in
    transcript order. The filter is the two content unions: the
    request side adds ``tool_result`` and ``attachment``, which an
    assistant turn never carries and ``AnthropicMessage`` will not
    validate, and unknown blocks go with them because
    ``_message_to_output`` skips those anyway.

    ``stop_reason`` follows the run's last block: a tool call means the
    model stopped to call it, anything else means it finished talking.
    ``guardrail_intervened`` is not decided here — it belongs to the
    denial path, and it comes from the verdict that was answered rather
    than from the shape of a block.
    """
    blocks: list[AnthropicContentBlock] = [
        block
        for msg in run
        for block in msg.content
        if isinstance(block, AnthropicTextBlock | AnthropicToolUseBlock | AnthropicThinkingBlock)
    ]
    stopped_to_call = bool(blocks) and isinstance(blocks[-1], AnthropicToolUseBlock)
    return AnthropicMessage(
        type="message",
        role="assistant",
        content=blocks,
        stop_reason="tool_use" if stopped_to_call else "end_turn",
    )


async def partial_event(
    frame: PromptFrame,
    *,
    request_id: str,
    signed_at: int,
    config: Config,
) -> AIInvocationObservedV1 | None:
    """The record frame N emits: the invocation its **previous** run answered.

    ``input`` is the transcript truncated before that run, which is what
    makes the shared helpers attribute ``used_tools`` and
    ``accessed_files`` to the round it consumed; ``output`` is the run
    itself. The fresh round drives the verdict and is the tail record's;
    it contributes nothing here. Nothing is left outstanding but the
    attachment digests a compliance listing supplies.

    ``request_id`` is the record's provisional address, computed by the
    addressing module and passed in — the frame cannot derive it, since
    the strong anchor lives in the trailing run this builder only reads
    tool names from. ``None`` when there is no trailing run to name (a
    first turn), and when ``actor.id`` is null: the server rejects an
    Anthropic identity with no identifier, so there is nothing useful to
    store.
    """
    split = split_transcript(frame)
    if not frame.actor.id or not split.assistant_run:
        return None
    normalized = await _normalized(split.before, answer=_answer(split.assistant_run), config=config)
    # The frame carries no tool definitions, so synthesize them from the
    # names this record's own transcript and its answer reveal — never
    # from the fresh round. Not cosmetic: without a declared tool whose
    # (server, name) key matches, ``used_tools_of`` cannot map a result to
    # a tool id and drops the entry.
    tools, servers = build_tools_declared(
        (name, None, None) for name in _tool_names([*split.before, *split.assistant_run])
    )
    normalized.input.tools_declared = tools
    normalized.input.tool_servers = servers
    return await build_event_from_normalized(
        normalized,
        EventEnvelope(
            request_id=request_id,
            timestamp=signed_at_iso(signed_at),
            identity_details=AnthropicIdentityDetails(user_id=frame.actor.id),
            model=AIModel(
                id=frame.model or "unknown", provider="anthropic", raw_model_id=frame.model
            ),
            parsed_as=PARSED_AS,
            user_agent=frame.source.application,
            conversation_id=frame.session_id,
        ),
        config=config,
    )
