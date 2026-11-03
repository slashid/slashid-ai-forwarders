"""The text of a deny, and the short reference that lets a result check recognize it."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from slashid_ai_forwarder_core.sink import join_deny_reasons

MAX_REASON = 500  # the Anthropic hook's bound, chosen
DEFAULT_REASON = "Blocked by your organization's policy."
_REF = re.compile(r" \(ref ([0-9a-f]{12})\)\Z")
_LABEL = b"slashid-agentcore-deny-ref-v1"


def derive_key(push_token: str) -> bytes:
    """One key per connection, same in every container; rotating the token voids old refs."""
    return hmac.new(push_token.encode(), _LABEL, hashlib.sha256).digest()


def _mac(text: str, key: bytes) -> str:
    return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()[:6].hex()


def with_reference(text: str, key: bytes) -> str:
    return f"{text} (ref {_mac(text, key)})"


def is_own_deny(text: str, key: bytes) -> bool:
    match = _REF.search(text)
    return match is not None and hmac.compare_digest(
        match.group(1), _mac(text[: match.start()], key)
    )


def deny_text(reasons: Sequence[str], key: bytes, *, prefix: str = "") -> str:
    """The reasons joined, behind ``prefix``, capped at 500 characters, then the reference."""
    text = prefix + (join_deny_reasons(reasons) or DEFAULT_REASON)
    if len(text) > MAX_REASON:
        text = text[: MAX_REASON - 1] + "…"
    return with_reference(text, key)


class _Block(BaseModel):
    """A content block with nothing but a type and a text."""

    model_config = ConfigDict(extra="forbid")
    type: str | None = None
    text: str


def single_text(content: JsonValue, structured: JsonValue) -> str | None:
    """The text of a result that is exactly one text block, else ``None``."""
    if structured is not None or not isinstance(content, list) or len(content) != 1:
        return None
    try:
        block = _Block.model_validate(content[0])
    except ValidationError:
        return None
    return block.text if block.type in (None, "text") else None
