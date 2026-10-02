import tomllib
from pathlib import Path

REQUIREMENTS = Path(__file__).parents[1] / "deploy" / "requirements.toml"


def test_allow_managed_hooks_only_is_top_level() -> None:
    # Codex ignores it under [hooks] (measured).
    requirements = tomllib.loads(REQUIREMENTS.read_text())
    assert requirements["allow_managed_hooks_only"] is True
    assert "allow_managed_hooks_only" not in requirements["hooks"]
