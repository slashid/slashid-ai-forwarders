import os

import pytest
from pydantic import ValidationError

from slashid_agentcore_forwarder.config import Config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [n for n in os.environ if n.startswith("SLASHID_")]:
        monkeypatch.delenv(name)


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t")
    c = Config()
    assert (c.harness_hooks_enabled, c.gateway_hooks_enabled) == (True, True)
    assert (c.verdict_fail_mode, c.verdict_budget_ms, c.shadow_mode) == ("allow", 3500, False)


def test_raw_content_is_always_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t")
    monkeypatch.setenv("SLASHID_INCLUDE_RAW_CONTENT", "true")
    assert Config().include_raw_content is False


@pytest.mark.parametrize("budget", ["99", "6501", "-1"])
def test_the_budget_stays_under_the_hook_timeout(
    monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t")
    monkeypatch.setenv("SLASHID_VERDICT_BUDGET_MS", budget)
    with pytest.raises(ValidationError):
        Config()


@pytest.mark.parametrize("budget", ["100", "6500"])
def test_the_budget_bounds_are_inclusive(monkeypatch: pytest.MonkeyPatch, budget: str) -> None:
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t")
    monkeypatch.setenv("SLASHID_VERDICT_BUDGET_MS", budget)
    assert Config().verdict_budget_ms == int(budget)


def test_a_bad_value_is_not_echoed_and_the_base_settings_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "t")
    monkeypatch.setenv("SLASHID_VERDICT_FAIL_MODE", "s3cr3t-value")
    with pytest.raises(ValidationError) as err:
        Config()
    assert "s3cr3t-value" not in str(err.value)
    assert Config.model_config["env_prefix"] == "SLASHID_"
    assert Config.model_config["frozen"] is True
