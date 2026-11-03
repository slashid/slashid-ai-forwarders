import pytest

from slashid_agentcore_forwarder.config import Config


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t")
    c = Config()
    assert (c.harness_hooks_enabled, c.gateway_hooks_enabled) == (True, True)
    assert (c.verdict_fail_mode, c.verdict_budget_ms, c.shadow_mode) == ("allow", 3500, False)


def test_raw_content_is_always_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t")
    monkeypatch.setenv("SLASHID_INCLUDE_RAW_CONTENT", "true")
    assert Config().include_raw_content is False
