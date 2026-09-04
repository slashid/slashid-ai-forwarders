"""Converse stopReason → AIStopReason mapping tests."""

from __future__ import annotations

from slashid_ai_forwarder_core.events import AIStopReason
from slashid_ai_forwarder_core.normalize.converse.stop_reasons import STOP_REASONS
from slashid_ai_forwarder_core.testing import yaml_pytest


@yaml_pytest()
def test_converse_stop_reasons(raw: str | None, expected: AIStopReason) -> None:
    assert STOP_REASONS.get(raw or "", "unknown") == expected
