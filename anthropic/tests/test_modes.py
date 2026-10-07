"""The three capability modes, end to end on the local platform: hook-only,
hook+compliance and compliance-only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.main import create_app
from slashid_anthropic_forwarder.platform import open_backends
from tests.conftest import SECRET, Signer
from tests.test_main import TOOL_FRAME

MODES: dict[str, dict[str, Any]] = {
    "hook-only": {"hook_signing_secret": SECRET},
    "hook+compliance": {
        "hook_signing_secret": SECRET,
        "compliance_key": "sk-ant-api01-x",
        "organization_uuid": "org-1",
    },
    "compliance-only": {"compliance_key": "sk-ant-api01-x", "organization_uuid": "org-1"},
}
HOOK = {"hook-only", "hook+compliance"}
COMPLIANCE = {"hook+compliance", "compliance-only"}
DENY_MARKER = "SLASHID_DENY_ME"


class Net:
    """Records every outbound call and answers an empty page to all of them."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.calls.append(f"{request.method} {request.url.host}{request.url.path}")
            return httpx.Response(200, json={"data": [], "has_more": False, "next_page": None})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def _records(data_dir: Path) -> list[dict[str, Any]]:
    db = sqlite3.connect(f"file:{data_dir / 'data.sqlite'}?mode=ro", uri=True)
    try:
        return [json.loads(doc) for (doc,) in db.execute("select doc from pending")]
    finally:
        db.close()


@pytest.mark.parametrize("mode", MODES)
async def test_mode(mode: str, tmp_path: Path, sign: Signer) -> None:
    config = Config(
        endpoint="https://api.slashid.com",
        push_token="t",
        platform="local",
        data_dir=str(tmp_path),
        tick_principal="s3cret",
        shadow_mode=False,
        capture_deny_marker=DENY_MARKER,
        **MODES[mode],
    )
    assert config.hook_enabled == (mode in HOOK)
    assert config.compliance_enabled == (mode in COMPLIANCE)
    net = Net()
    app = create_app(config, backends=lambda: open_backends(config), client=net.client())
    body = json.dumps(TOOL_FRAME).encode()
    text = {"type": "text", "text": DENY_MARKER}
    marked = json.dumps({**TOOL_FRAME, "messages": [{"role": "user", "content": [text]}]}).encode()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            forged = {**sign(body, "msg_forged"), "webhook-signature": "v1,AAAA"}
            assert (await c.post("/", content=body)).status_code == 401
            assert (await c.post("/", content=body, headers=forged)).status_code == 401

            allowed = await c.post("/", content=body, headers=sign(body, "msg_allowed"))
            if mode in HOOK:
                assert (allowed.status_code, allowed.json()) == (200, {"action": "allow"})
                records = _records(tmp_path)
                assert records
                assert not any(r.get("awaiting") for r in records)  # a plain frame never waits

                denied = await c.post("/", content=marked, headers=sign(marked, "msg_denied"))
                assert denied.status_code == 200 and denied.json()["action"] != "allow"
                denial = [r for r in _records(tmp_path) if "msg_denied" in r.get("webhook_ids", [])]
                assert denial
                # Only a deployment with readers has anyone to confirm the denial against.
                waits = any("denial_activity" in r.get("awaiting", []) for r in denial)
                assert waits == (mode == "hook+compliance")
            else:
                assert allowed.status_code == 401  # no secret configured to verify it

            assert (await c.post("/tick")).status_code == 401
            assert (
                await c.post("/tick", headers={"authorization": "Bearer no"})
            ).status_code == 401
            before = len(net.calls)
            tick = await c.post("/tick", headers={"authorization": "Bearer s3cret"})
            assert tick.status_code == 200
            readers = [call for call in net.calls[before:] if "anthropic.com" in call]
            assert bool(readers) == (mode in COMPLIANCE)
