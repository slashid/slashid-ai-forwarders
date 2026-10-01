"""OpenAI Responses wire-schema validation against Bedrock MIL captures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    Response,
    ResponsesFunctionCall,
    ResponsesItem,
    ResponsesMessage,
    ResponsesPart,
    ResponsesReasoning,
    ResponsesRequest,
    ResponseStreamEvent,
    ResponsesUnknownItem,
    ResponsesUnknownPart,
    final_response,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_EVENTS = TypeAdapter(list[ResponseStreamEvent])


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())


def test_plain_fixture_validates() -> None:
    record = _load("openai_responses_mil.json")
    request = ResponsesRequest.model_validate(record["input"]["inputBodyJson"])
    assert request.input == "Say hello in three words."
    response = Response.model_validate(record["output"]["outputBodyJson"])
    assert response.object == "response"
    assert response.status == "completed"
    assert any(isinstance(i, ResponsesReasoning) for i in response.output)
    assert any(isinstance(i, ResponsesMessage) for i in response.output)


def test_stream_fixture_validates() -> None:
    record = _load("openai_responses_stream_mil.json")
    request = ResponsesRequest.model_validate(record["input"]["inputBodyJson"])
    assert isinstance(request.input, list)
    assert [type(i) for i in request.input] == [ResponsesMessage, ResponsesMessage]
    assert request.tools[0].name == "get_weather"
    events = _EVENTS.validate_python(record["output"]["outputBodyJson"])
    final = final_response(events)
    assert final is not None
    assert final.status == "completed"
    calls = [i for i in final.output if isinstance(i, ResponsesFunctionCall)]
    assert [c.name for c in calls] == ["get_weather"]


def test_final_response_none_without_terminal_event() -> None:
    assert final_response([ResponseStreamEvent(type="error")]) is None


def test_anthropic_request_is_not_a_responses_request() -> None:
    with pytest.raises(ValidationError):
        ResponsesRequest.model_validate({"messages": [{"role": "user", "content": "hi"}]})


def test_anthropic_message_is_not_a_response() -> None:
    with pytest.raises(ValidationError):
        Response.model_validate(
            {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "x"}]}
        )


def test_unknown_item_and_part() -> None:
    item = TypeAdapter(ResponsesItem).validate_python({"type": "image_generation_call", "id": "x"})
    assert isinstance(item, ResponsesUnknownItem)
    part = TypeAdapter(ResponsesPart).validate_python({"type": "refusal", "refusal": "no"})
    assert isinstance(part, ResponsesUnknownPart)
