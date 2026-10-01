from __future__ import annotations

import pytest

from slashid_codex.http import make_client


async def test_client_ignores_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    async with make_client(timeout_seconds=7.0) as client:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client._mounts == {}
        assert client.timeout.connect == 7.0
        assert client.timeout.read == 7.0
