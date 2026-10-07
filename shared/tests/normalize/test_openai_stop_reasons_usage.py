"""OpenAI Responses stop-reason mapping and additive token accounting."""

from __future__ import annotations

import pytest

from slashid_ai_forwarder_core.events import AIInvocationTokens, AIStopReason
from slashid_ai_forwarder_core.normalize.openai.chat.schema import (
    ChatCompletionTokensDetails,
    ChatPromptTokensDetails,
    ChatUsage,
)
from slashid_ai_forwarder_core.normalize.openai.responses.schema import (
    ResponsesInputTokensDetails,
    ResponsesOutputTokensDetails,
    ResponsesUsage,
)
from slashid_ai_forwarder_core.normalize.openai.stop_reasons import (
    chat_stop_reason,
    responses_stop_reason,
)
from slashid_ai_forwarder_core.normalize.openai.usage import (
    additive_tokens,
    chat_usage_to_tokens,
    responses_usage_to_tokens,
)


@pytest.mark.parametrize(
    ("status", "reason", "has_tool_call", "expected"),
    [
        ("completed", None, True, "tool_use"),
        ("completed", None, False, "end_turn"),
        ("incomplete", "max_output_tokens", False, "max_tokens"),
        ("incomplete", "content_filter", False, "content_filtered"),
        ("incomplete", None, False, "unknown"),
        ("failed", None, False, "error"),
        ("in_progress", None, False, "unknown"),
        (None, None, False, "unknown"),
    ],
)
def test_responses_stop_reason(
    status: str | None, reason: str | None, has_tool_call: bool, expected: AIStopReason
) -> None:
    assert responses_stop_reason(status, reason, has_tool_call=has_tool_call) == expected


def test_additive_tokens_cache_write() -> None:
    assert additive_tokens(
        input_total=15189, cached=0, cache_write=15186, output_total=43, reasoning=0
    ) == AIInvocationTokens(input=3, cache_read=0, cache_write=15186, output=43, reasoning=0)


def test_additive_tokens_reasoning() -> None:
    assert additive_tokens(
        input_total=12, cached=0, cache_write=0, output_total=23, reasoning=12
    ) == AIInvocationTokens(input=12, output=11, reasoning=12)


def test_additive_tokens_clamps_at_zero() -> None:
    assert additive_tokens(
        input_total=1, cached=5, cache_write=0, output_total=1, reasoning=3
    ) == AIInvocationTokens(input=0, cache_read=5, output=0, reasoning=3)


def test_responses_usage_to_tokens() -> None:
    usage = ResponsesUsage(
        input_tokens=100,
        output_tokens=30,
        input_tokens_details=ResponsesInputTokensDetails(cached_tokens=60, cache_write_tokens=20),
        output_tokens_details=ResponsesOutputTokensDetails(reasoning_tokens=10),
    )
    assert responses_usage_to_tokens(usage) == AIInvocationTokens(
        input=20, cache_read=60, cache_write=20, output=20, reasoning=10
    )


def test_responses_usage_to_tokens_missing() -> None:
    assert responses_usage_to_tokens(None) == AIInvocationTokens()
    assert responses_usage_to_tokens(ResponsesUsage(input_tokens=5)) == AIInvocationTokens(input=5)


@pytest.mark.parametrize(
    ("finish_reason", "expected"),
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("function_call", "tool_use"),
        ("content_filter", "content_filtered"),
        ("something_new", "unknown"),
        (None, "unknown"),
    ],
)
def test_chat_stop_reason(finish_reason: str | None, expected: AIStopReason) -> None:
    assert chat_stop_reason(finish_reason) == expected


def test_chat_usage_cache_and_reasoning() -> None:
    usage = ChatUsage(
        prompt_tokens=24318,
        completion_tokens=40,
        prompt_tokens_details=ChatPromptTokensDetails(cached_tokens=24316),
        completion_tokens_details=ChatCompletionTokensDetails(reasoning_tokens=30),
    )
    assert chat_usage_to_tokens(usage) == AIInvocationTokens(
        input=2, cache_read=24316, cache_write=0, output=10, reasoning=30
    )


def test_chat_usage_without_details() -> None:
    assert chat_usage_to_tokens(ChatUsage(prompt_tokens=123, completion_tokens=46)) == (
        AIInvocationTokens(input=123, output=46)
    )


def test_chat_usage_none() -> None:
    assert chat_usage_to_tokens(None) == AIInvocationTokens()
