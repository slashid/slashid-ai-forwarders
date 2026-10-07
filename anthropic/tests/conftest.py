"""Shared fixtures: a signing secret and a signer producing Anthropic's headers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from collections.abc import Callable
from typing import Any

import pytest

from slashid_anthropic_forwarder import main

SECRET = "whsec_" + base64.b64encode(bytes([0xFB, 0xFF, 0xBF]) * 8).decode()

Signer = Callable[[bytes, str], dict[str, str]]


@pytest.fixture
def secret() -> str:
    return SECRET


@pytest.fixture
def sign() -> Signer:
    def _sign(body: bytes, msg_id: str = "req_test") -> dict[str, str]:
        ts = str(int(time.time()))
        key = base64.b64decode(SECRET.removeprefix("whsec_"))
        payload = f"{msg_id}.{ts}.".encode() + body
        sig = "v1," + base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode()
        return {
            "webhook-id": msg_id,
            "webhook-timestamp": ts,
            "webhook-signature": sig,
            "content-type": "application/json",
        }

    return _sign


@pytest.fixture
def fast_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    real = asyncio.sleep

    async def quick(_seconds: float) -> None:
        await real(0.005)

    monkeypatch.setattr(main, "sleep", quick)


async def _until(done: Callable[[], bool], *, seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not done():
            await asyncio.sleep(0.005)


async def _no_readers(**_: Any) -> dict[str, int]:
    return {}
