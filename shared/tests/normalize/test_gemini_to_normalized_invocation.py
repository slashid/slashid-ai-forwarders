"""End-to-end YAML-driven tests for gemini.normalize.to_normalized_invocation.

Fixtures drawn from the Vertex POC (2026-09-04) plus synthetic
attachment / server-side variants. Focuses on the joint request+response
→ NormalizedInvocation shape; the tool_use_id synthesis invariants have
a dedicated test in ``test_gemini_tool_use_id_stability.py``.
"""

from __future__ import annotations

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.gemini.normalize import to_normalized_invocation
from slashid_ai_forwarder_core.normalize.gemini.schema import (
    GeminiRequestBody,
    GeminiResponse,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedInvocation
from slashid_ai_forwarder_core.testing import yaml_pytest

_CONFIG = BaseConfig(endpoint="http://test", push_token="test")


@yaml_pytest()
async def test_gemini_to_normalized_invocation(
    req: GeminiRequestBody,
    response: GeminiResponse,
    expected: NormalizedInvocation,
) -> None:
    assert await to_normalized_invocation(req, response, config=_CONFIG) == expected
