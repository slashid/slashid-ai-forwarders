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

from slashid_ai_forwarder_core.content_utils import truncate_middle
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    message_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicAttachmentBlock,
    AnthropicMessage,
    AnthropicRequestBody,
    AnthropicRequestMessage,
    AnthropicTextBlock,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.normalized.media_types import parse_media_type
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.normalize.turn import after_last_assistant

from ..config import Config

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


def _name_for(block: AnthropicAttachmentBlock, candidates: list[str]) -> str | None:
    """Consume a listed name for this block. Consuming matters: two uploads
    of the same media type must not both claim the first listed name."""
    if block.file_name:
        if block.file_name in candidates:
            candidates.remove(block.file_name)
        return block.file_name
    for i, name in enumerate(candidates):
        guessed, _ = mimetypes.guess_type(name)
        if guessed == block.media_type:
            return candidates.pop(i)
    return None


def attachment_files(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> list[AIAccessedFile]:
    """One entry per text-bearing attachment in the last round of ``messages``.

    Which round that is belongs to the caller: the whole frame scans the
    fresh round, ``split.before`` scans the round the previous run
    consumed.

    A hook attachment carries extracted text and never bytes, so the
    digest and ``byte_length`` are over that text — not over the frame's
    ``size_bytes``, which describes the upload and disagrees with it
    whenever Claude stored a processed copy. An attachment with no text
    (an image) yields no entry: there is nothing to hash.
    """
    last_round = list(after_last_assistant(messages))
    candidates = _uploaded_names(last_round)
    out: list[AIAccessedFile] = []
    for msg in last_round:
        for block in msg.content:
            if not isinstance(block, AnthropicAttachmentBlock) or block.text is None:
                continue
            data = block.text.encode()
            out.append(
                AIAccessedFile(
                    name=_name_for(block, candidates),
                    content_hashes={
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "sha1": hashlib.sha1(data).hexdigest(),
                        "md5": hashlib.md5(data).hexdigest(),
                    },
                    media_type=parse_media_type(block.media_type),
                    byte_length=len(data),
                    redacted_content=(
                        truncate_middle(block.text, config.max_content_size)
                        if config.include_raw_content
                        else None
                    ),
                    provenance="attachment",
                )
            )
    return out


async def _normalized(
    messages: list[AnthropicRequestMessage], *, config: Config
) -> NormalizedInvocation:
    """Canonicalize the messages it is given, with an empty response.

    Empty because this path only needs the file side, and the round it
    is scanning has not been answered. Task 3.4 gives the function the
    run that answered. ``finalize`` then adds the last round's
    tool-result files behind the attachment entries and dedupes the
    union, first-seen wins.
    """
    normalized = await message_to_normalized_invocation(
        AnthropicRequestBody(messages=messages),
        AnthropicMessage(type="message", role="assistant", content=[]),
        config=config,
    )
    normalized.accessed_files = attachment_files(messages, config=config)
    return finalize(normalized, config=config)


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
