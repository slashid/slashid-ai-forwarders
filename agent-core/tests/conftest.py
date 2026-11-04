import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import JsonValue

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def load() -> Callable[[str], dict[str, JsonValue]]:
    """A payload fixture by name; each call returns a fresh copy."""

    def _load(name: str) -> dict[str, JsonValue]:
        return json.loads((FIXTURES / f"{name}.json").read_text())

    return _load


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """The ``live`` tests talk to a real SlashID server: collected always, run only on request."""
    if os.environ.get("SLASHID_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live checks run only with SLASHID_LIVE=1")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)
