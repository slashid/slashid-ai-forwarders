"""The outbound client to SlashID."""

from __future__ import annotations

import ssl

import httpx


def make_client(*, timeout_seconds: float = 10.0) -> httpx.AsyncClient:
    """No proxy, CA bundle or ``.netrc`` from the environment (hooks inherit
    the user's), no redirects, the system's CAs."""
    return httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        verify=ssl.create_default_context(),
        timeout=httpx.Timeout(timeout_seconds),
    )
