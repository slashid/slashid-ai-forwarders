"""Anthropic stop_reason → AIStopReason mapping tests."""

from __future__ import annotations

from slashid_ai_forwarder_core.events import AIStopReason
from slashid_ai_forwarder_core.normalize.anthropic.stop_reasons import (
    map as map_anthropic_stop_reason,
)
from slashid_ai_forwarder_core.testing import yaml_pytest


@yaml_pytest()
def test_anthropic_stop_reasons(raw: str | None, expected: AIStopReason) -> None:
    assert map_anthropic_stop_reason(raw) == expected
