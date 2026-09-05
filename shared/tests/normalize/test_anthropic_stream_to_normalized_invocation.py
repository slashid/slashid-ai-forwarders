"""End-to-end YAML-driven tests for anthropic.normalize.stream_to_normalized_invocation."""

from __future__ import annotations

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.anthropic.normalize import (
    stream_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.anthropic.schema import (
    AnthropicRequestBody,
    AnthropicStreamEvent,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.testing import yaml_pytest

_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


@yaml_pytest()
async def test_anthropic_stream_to_normalized_invocation(
    req: AnthropicRequestBody,
    response: list[AnthropicStreamEvent],
    expected: NormalizedInvocation,
) -> None:
    # ``req`` (not ``request``) — pytest reserves ``request`` as a fixture name
    # and rejects it in @pytest.mark.parametrize.
    assert await stream_to_normalized_invocation(req, response, config=_CONFIG) == expected
