"""What the person typed in a round, apart from everything else it carries.

A fresh round holds more than a prompt: what tools returned, uploads, and
text the client wrote on the person's behalf. SlashID infers what the
person intends from their own words, and only those: text a tool or a file
supplied can be written by someone else.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicRequestMessage,
    AnthropicTextBlock,
)

# Blocks the client writes around the prompt: its own context, and the
# listing of the files a message uploads. A block is one of these only when
# the wrapper is all there is to it, so a person who mentions a tag keeps
# their sentence.
_CLIENT_WRAPPERS = ("system-reminder", "uploaded_files")
_CLIENT_BLOCK = re.compile(
    r"\A\s*<(?P<tag>" + "|".join(_CLIENT_WRAPPERS) + r")>.*</(?P=tag)>\s*\Z", re.DOTALL
)


def typed_prompt(fresh: Sequence[AnthropicRequestMessage]) -> str | None:
    """The text blocks of the round's user messages the client did not write,
    one paragraph each; None when the person typed nothing."""
    typed = [
        block.text.strip()
        for message in fresh
        if message.role == "user"
        for block in message.content
        if isinstance(block, AnthropicTextBlock)
        and block.text.strip()
        and not _CLIENT_BLOCK.match(block.text)
    ]
    return "\n\n".join(typed) or None
