import os
from collections.abc import Iterator

import pytest

from helpers import make_config
from slashid_agentcore_forwarder.checks import Checker
from slashid_agentcore_forwarder.config import load_config
from slashid_agentcore_forwarder.deny import derive_key
from slashid_agentcore_forwarder.roles import Roles
from slashid_agentcore_forwarder.runtime import (
    INPUT_MAX_ENTRIES,
    build_runtime,
    get_runtime,
    reset_runtime,
)
from slashid_agentcore_forwarder.tool_call import CachedInput


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in [n for n in os.environ if n.startswith("SLASHID_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "env-token")
    monkeypatch.setenv("SLASHID_ENDPOINT", "http://env")
    load_config.cache_clear()
    reset_runtime()
    yield
    load_config.cache_clear()
    reset_runtime()


def test_build_runtime_wires_the_config() -> None:
    config = make_config(push_token="the-token")
    built = build_runtime(config)
    assert built.config is config
    assert built.key == derive_key("the-token")
    assert isinstance(built.checker, Checker)
    assert isinstance(built.roles, Roles)


def test_build_runtime_gives_each_runtime_its_own_cache() -> None:
    first, second = build_runtime(make_config()), build_runtime(make_config())
    first.inputs.put("k", CachedInput(tool_name="t", tool_input=None))
    assert first.inputs.get("k") is not None
    assert second.inputs.get("k") is None


def test_the_input_cache_is_bounded() -> None:
    cache = build_runtime(make_config()).inputs
    for n in range(INPUT_MAX_ENTRIES + 1):
        cache.put(str(n), CachedInput(tool_name="t", tool_input=None))
    assert cache.get("0") is None
    assert cache.get("1") is not None


def test_get_runtime_builds_from_the_environment_once() -> None:
    first = get_runtime()
    assert first.config.endpoint == "http://env"
    assert first.key == derive_key("env-token")
    assert get_runtime() is first


def test_reset_runtime_builds_a_new_one() -> None:
    first = get_runtime()
    reset_runtime()
    assert get_runtime() is not first
