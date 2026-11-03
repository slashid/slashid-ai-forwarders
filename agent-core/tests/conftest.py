import json
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
