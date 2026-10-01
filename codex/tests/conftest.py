from __future__ import annotations

from pathlib import Path

import pytest
from slashid_ai_forwarder_core.events import AIInvocationObservedV1, AIModel, OpenAIIdentityDetails

from slashid_codex.config import CodexConfig

TOKEN = "t" * 32


@pytest.fixture
def make_config(tmp_path: Path):
    def _make(**overrides: object) -> CodexConfig:
        token_file = tmp_path / "token"
        token_file.write_text(TOKEN)
        values: dict[str, object] = {
            "endpoint": "https://api.example.test",
            "push_token_file": str(token_file),
            "user_id": "user-abc",
            "codex_home": str(tmp_path / ".codex"),
            **overrides,
        }
        return CodexConfig(**values)  # ty: ignore[invalid-argument-type]

    return _make


@pytest.fixture
def invocation() -> AIInvocationObservedV1:
    return AIInvocationObservedV1(
        request_id="turn-1",
        timestamp="2026-09-30T15:33:37.000Z",
        identity_details=OpenAIIdentityDetails(user_id="user-abc"),
        model=AIModel(id="gpt-6-astra", provider="openai"),
        parsed_as="codex-hook",
    )
