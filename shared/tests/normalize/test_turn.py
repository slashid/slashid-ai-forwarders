"""Tests for the fresh-turn helper in ``normalize/turn.py``.

Only the boundary rule is exercised here — the three call sites
(``events._used_tools``, ``tool_results.extract_tool_result_files``,
``converse.attachments.extract_attachments``) have their own end-to-end
coverage confirming they still filter correctly after migrating to this
helper.
"""

from __future__ import annotations

from dataclasses import dataclass

from slashid_ai_forwarder_core.normalize.turn import after_last_assistant


@dataclass
class _Msg:
    role: str


def test_after_last_assistant_returns_tail() -> None:
    msgs = [_Msg("user"), _Msg("assistant"), _Msg("user"), _Msg("user")]
    assert after_last_assistant(msgs) == [_Msg("user"), _Msg("user")]


def test_after_last_assistant_finds_last_of_multiple() -> None:
    msgs = [
        _Msg("user"),
        _Msg("assistant"),
        _Msg("user"),
        _Msg("assistant"),
        _Msg("user"),
    ]
    assert after_last_assistant(msgs) == [_Msg("user")]


def test_after_last_assistant_returns_all_when_no_assistant() -> None:
    msgs = [_Msg("user"), _Msg("system"), _Msg("user")]
    assert after_last_assistant(msgs) == msgs


def test_after_last_assistant_returns_empty_when_assistant_is_last() -> None:
    msgs = [_Msg("user"), _Msg("assistant")]
    assert after_last_assistant(msgs) == []


def test_after_last_assistant_returns_empty_on_empty_input() -> None:
    assert after_last_assistant([]) == []
