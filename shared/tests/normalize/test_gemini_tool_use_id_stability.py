"""Locks in the Gemini tool_use_id synthesis invariants.

Gemini's ``functionCall`` emits no correlation id; the normalizer
synthesizes one from ``sha256(name || args || turn_index || part_index)``.
The critical invariant is that a call seen response-side on turn N and
that same call replayed request-side (in a follow-up request's
``contents[]``) at position turn=N produce IDENTICAL ids — otherwise
``used_tools`` extraction on the follow-up event breaks.

These tests exercise ``to_normalized_invocation`` end-to-end rather than
poking the private synth function directly — the invariant is about
what the public API produces.
"""

from __future__ import annotations

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.gemini.normalize import to_normalized_invocation
from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)

_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


async def test_response_call_id_matches_follow_up_request_replay() -> None:
    """The functionCall emitted in response-side turn=len(input.messages)
    part=0 has the same synthetic id as the same functionCall seen
    request-side in a follow-up conversation."""
    # Turn 1 (call A → response with functionCall).
    req_1 = GeminiRequestBody.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "weather?"}]}]}
    )
    resp_1 = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    norm_1 = await to_normalized_invocation(req_1, resp_1, config=_CONFIG)
    assert norm_1.output.message is not None
    call_id_from_response = norm_1.output.message.content[0].tool_use_id
    assert call_id_from_response is not None

    # Turn 2 (call B → follow-up request has [user, model_call, user_response]).
    req_2 = GeminiRequestBody.model_validate(
        {
            "contents": [
                {"role": "user", "parts": [{"text": "weather?"}]},
                {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "get_weather",
                                "response": {"weather": "sunny"},
                            }
                        }
                    ],
                },
            ]
        }
    )
    resp_2 = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "It's sunny."}]},
                    "finishReason": "STOP",
                }
            ]
        }
    )
    norm_2 = await to_normalized_invocation(req_2, resp_2, config=_CONFIG)
    call_id_from_replay = norm_2.input.messages[1].content[0].tool_use_id
    response_id_from_replay = norm_2.input.messages[2].content[0].tool_use_id

    # Both replay ids correlate (input's functionCall and functionResponse pair
    # via the FIFO queue) AND match the response-side id from turn 1.
    assert call_id_from_replay == response_id_from_replay
    assert call_id_from_replay == call_id_from_response


async def test_different_args_produce_different_ids() -> None:
    """Same tool name + different args → different ids."""
    resp_paris = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    resp_london = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"functionCall": {"name": "get_weather", "args": {"city": "London"}}}
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    empty_req = GeminiRequestBody.model_validate({"contents": []})
    norm_paris = await to_normalized_invocation(empty_req, resp_paris, config=_CONFIG)
    norm_london = await to_normalized_invocation(empty_req, resp_london, config=_CONFIG)
    assert norm_paris.output.message is not None
    assert norm_london.output.message is not None
    assert (
        norm_paris.output.message.content[0].tool_use_id
        != norm_london.output.message.content[0].tool_use_id
    )


async def test_different_turn_index_produces_different_ids() -> None:
    """Same (name, args) at different turn positions → different ids.

    Two calls to the same tool in different turns of the same conversation
    stay distinguishable — matters for ``used_tools`` extraction on multi-
    round tool use.
    """
    resp = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": "get_time", "args": {}}}],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    req_short = GeminiRequestBody.model_validate(
        {"contents": [{"role": "user", "parts": [{"text": "?"}]}]}
    )  # output_turn_index = 1
    req_long = GeminiRequestBody.model_validate(
        {
            "contents": [
                {"role": "user", "parts": [{"text": "?"}]},
                {"role": "model", "parts": [{"text": "hi"}]},
                {"role": "user", "parts": [{"text": "again?"}]},
            ]
        }
    )  # output_turn_index = 3

    norm_short = await to_normalized_invocation(req_short, resp, config=_CONFIG)
    norm_long = await to_normalized_invocation(req_long, resp, config=_CONFIG)
    assert norm_short.output.message is not None
    assert norm_long.output.message is not None
    assert (
        norm_short.output.message.content[0].tool_use_id
        != norm_long.output.message.content[0].tool_use_id
    )


async def test_parallel_same_name_calls_fifo_pair_correctly() -> None:
    """Two concurrent functionCalls with the same name in one model turn
    pair to their functionResponses in FIFO order — nth response matches
    nth call (by args order)."""
    req = GeminiRequestBody.model_validate(
        {
            "contents": [
                {"role": "user", "parts": [{"text": "compare"}]},
                {
                    "role": "model",
                    "parts": [
                        {"functionCall": {"name": "get_weather", "args": {"city": "Paris"}}},
                        {"functionCall": {"name": "get_weather", "args": {"city": "London"}}},
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "get_weather",
                                "response": {"weather": "sunny"},
                            }
                        },
                        {
                            "functionResponse": {
                                "name": "get_weather",
                                "response": {"weather": "cloudy"},
                            }
                        },
                    ],
                },
            ]
        }
    )
    empty_resp = GeminiResponse.model_validate(
        {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "STOP"}]}
    )
    norm = await to_normalized_invocation(req, empty_resp, config=_CONFIG)

    call_paris_id = norm.input.messages[1].content[0].tool_use_id
    call_london_id = norm.input.messages[1].content[1].tool_use_id
    response_1_id = norm.input.messages[2].content[0].tool_use_id
    response_2_id = norm.input.messages[2].content[1].tool_use_id

    # FIFO pairing: 1st response ↔ 1st call, 2nd response ↔ 2nd call.
    assert response_1_id == call_paris_id
    assert response_2_id == call_london_id
    # Two same-name-different-args calls remain distinct.
    assert call_paris_id != call_london_id


async def test_code_execution_result_pairs_with_preceding_executable_code() -> None:
    """Server-side ``codeExecutionResult`` shares tool_use_id with the
    ``executableCode`` immediately preceding it in the same parts[].
    Enables ``used_tools`` correlation for server-side tool blocks the
    same way client-side functionCall/functionResponse pairs do."""
    empty_req = GeminiRequestBody.model_validate({"contents": []})
    resp = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"executableCode": {"language": "PYTHON", "code": "print(2+2)"}},
                            {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "4\n"}},
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    norm = await to_normalized_invocation(empty_req, resp, config=_CONFIG)
    assert norm.output.message is not None
    exec_id = norm.output.message.content[0].tool_use_id
    result_id = norm.output.message.content[1].tool_use_id
    assert exec_id is not None
    assert result_id == exec_id


async def test_orphan_code_execution_result_synthesizes_fallback_id() -> None:
    """A ``codeExecutionResult`` with no preceding ``executableCode`` in
    the same parts[] gets a position-derived fallback id — non-crashing,
    non-correlating. Matches orphan functionResponse behaviour."""
    empty_req = GeminiRequestBody.model_validate({"contents": []})
    resp = GeminiResponse.model_validate(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"codeExecutionResult": {"outcome": "OUTCOME_OK", "output": "4\n"}},
                        ],
                    },
                    "finishReason": "STOP",
                }
            ]
        }
    )
    norm = await to_normalized_invocation(empty_req, resp, config=_CONFIG)
    assert norm.output.message is not None
    sid = norm.output.message.content[0].tool_use_id
    assert sid is not None
    assert sid.startswith("gemini-")


async def test_orphan_function_response_synthesizes_fallback_id() -> None:
    """A functionResponse with no matching prior functionCall gets a
    non-correlating synthetic id — doesn't crash, doesn't collide with
    a real call id."""
    req = GeminiRequestBody.model_validate(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "get_weather",
                                "response": {"weather": "sunny"},
                            }
                        }
                    ],
                }
            ]
        }
    )
    empty_resp = GeminiResponse.model_validate(
        {"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "STOP"}]}
    )
    norm = await to_normalized_invocation(req, empty_resp, config=_CONFIG)
    sid = norm.input.messages[0].content[0].tool_use_id
    assert sid is not None
    assert sid.startswith("gemini-")
