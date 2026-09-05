"""End-to-end YAML-driven tests for converse.normalize.to_normalized_invocation."""

from __future__ import annotations

from slashid_ai_forwarder_core.normalize.converse.normalize import to_normalized_invocation
from slashid_ai_forwarder_core.normalize.converse.schema import (
    ConverseRequestBody,
    ConverseResponse,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.testing import yaml_pytest


@yaml_pytest()
def test_converse_to_normalized_invocation(
    req: ConverseRequestBody,
    response: ConverseResponse,
    expected: NormalizedInvocation,
) -> None:
    # ``req`` (not ``request``) — pytest reserves ``request`` as a fixture name
    # and rejects it in @pytest.mark.parametrize.
    assert to_normalized_invocation(req, response) == expected


# ==========================================================================
# converse_dict_to_normalized (temporary Chunk-7 adapter, deleted in Chunk 8)
# ==========================================================================


def test_converse_dict_to_normalized_from_full_record() -> None:
    """Adapter takes the mil_normalize post-Phase-1.1 record shape (dict) and
    produces a NormalizedInvocation. Deleted in Chunk 8 once mil_normalize
    emits NormalizedInvocation directly."""
    from slashid_ai_forwarder_core.normalize.converse.normalize import (
        converse_dict_to_normalized,
    )

    record = {
        "input": {
            "inputBodyJson": {
                "messages": [{"role": "user", "content": [{"text": "hi"}]}],
            }
        },
        "output": {
            "outputBodyJson": {
                "output": {"message": {"role": "assistant", "content": [{"text": "hi back"}]}},
                "stopReason": "end_turn",
            }
        },
    }
    normalized = converse_dict_to_normalized(record)
    assert normalized.output.stop_reason == "end_turn"
    assert normalized.output.message is not None
    assert normalized.output.message.content[0].text == "hi back"
    # Input is best-effort — schemas may reject drift; missing input yields
    # an empty NormalizedInvocationInput, not an exception.
    assert normalized.input.messages is not None
    assert normalized.input.messages[0].role == "user"


def test_converse_dict_to_normalized_missing_input_body_yields_empty_input() -> None:
    """Missing / non-Converse input body: input side falls back to empty
    NormalizedInvocationInput. Output side still parses if present."""
    from slashid_ai_forwarder_core.normalize.converse.normalize import (
        converse_dict_to_normalized,
    )

    record = {
        "input": {"inputBodyJson": None},
        "output": {
            "outputBodyJson": {
                "output": {"message": {"role": "assistant", "content": [{"text": "hi"}]}},
                "stopReason": "end_turn",
            }
        },
    }
    normalized = converse_dict_to_normalized(record)
    assert normalized.input.messages == []
    assert normalized.output.stop_reason == "end_turn"
