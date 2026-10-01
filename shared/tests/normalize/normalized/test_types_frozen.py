import pytest
from pydantic import ValidationError

from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedMessage,
)


def test_message_is_frozen() -> None:
    msg = NormalizedMessage(role="user", content=[NormalizedContent(kind="text", text="hi")])
    with pytest.raises(ValidationError):
        msg.role = "assistant"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        msg.content[0].text = "changed"  # type: ignore[misc]


def test_compaction_kind_is_accepted() -> None:
    block = NormalizedContent(kind="compaction", text="ab" * 32)
    assert block.kind == "compaction"
