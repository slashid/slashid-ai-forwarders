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
