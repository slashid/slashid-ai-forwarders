"""The tool call frame: one verdict for all the calls in a response, judged
before any of them runs."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from slashid_ai_forwarder_core.events import AIInvocationObservedV1

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.frame import ToolCallFrame
from slashid_anthropic_forwarder.hook.toolcall import tool_call_event
from slashid_anthropic_forwarder.hook.verdict import RECOVERY
from slashid_anthropic_forwarder.main import create_app
from tests.conftest import SECRET, Signer
from tests.test_pending import a_store, addresses

SIGNED_AT = 1_700_000_000


def use(name: str, id: str = "toolu_1", info: dict[str, Any] | None = None, **input: Any):
    block: dict[str, Any] = {"type": "tool_use", "id": id, "tool_name": name, "input": input}
    if info is not None:
        block["tool_info"] = info
    return block


def frame(*blocks: dict[str, Any], **over: Any) -> dict[str, Any]:
    """A tool call frame as Anthropic sends it: one assistant message."""
    text = {"type": "text", "text": "Running it."}
    return {
        "type": "tool_call",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "session_id": "sess_1",
        "request_id": "msg_1-tool_call",
        "model": "claude-sonnet-5-5",
        "actor": {"type": "user", "id": "user_01A", "email_address": "a@example.com"},
        "source": {"application": "claude-code"},
        "metadata": {},
        "messages": [{"role": "assistant", "content": [text, *blocks]}],
    } | over


def event(*blocks: dict[str, Any], **over: Any) -> AIInvocationObservedV1 | None:
    parsed = ToolCallFrame.model_validate(frame(*blocks, **over))
    return tool_call_event(parsed, signed_at=SIGNED_AT)


CLIENT = {"type": "client"}


def test_the_frame_lists_the_tool_uses_of_the_last_message() -> None:
    parsed = ToolCallFrame.model_validate(
        frame(use("Bash", "toolu_a", CLIENT, command="ls"), use("Read", "toolu_b", CLIENT))
    )
    assert [(u.id, u.tool_name) for u in parsed.tool_uses()] == [
        ("toolu_a", "Bash"),
        ("toolu_b", "Read"),
    ]


def test_a_tool_use_without_tool_info_is_a_client_tool() -> None:
    [one] = ToolCallFrame.model_validate(frame(use("Bash"))).tool_uses()
    assert one.tool_info.type == "client"


def test_the_event_names_the_requested_tool_and_declares_it() -> None:
    built = event(use("Bash", "toolu_a", CLIENT, command="ls"))
    assert built is not None
    assert built.request_id == "msg_1-tool_call"
    assert built.conversation_id == "sess_1"
    assert built.identity_details.user_id == "user_01A"  # ty: ignore[unresolved-attribute]
    assert built.model.id == "claude-sonnet-5-5"
    [requested] = built.requested_tool_uses or []
    [tool] = built.available_tools or []
    [server] = built.available_tool_servers or []
    assert requested.tool_use_id == "toolu_a"
    assert requested.tool_id == tool.id and tool.tool_server_id == server.id
    assert (tool.name, server.name) == ("Bash", "builtin")
    assert not built.used_tools  # nothing has run yet


def test_a_local_mcp_tool_resolves_to_its_server() -> None:
    built = event(use("mcp__crm__search", info=CLIENT))
    assert built is not None
    [tool] = built.available_tools or []
    [server] = built.available_tool_servers or []
    assert (tool.name, server.name, server.kind) == ("search", "crm", "mcp")


def test_a_third_party_tool_takes_its_server_from_the_toolset() -> None:
    info = {"type": "third_party", "toolset_name": "crm", "origin": "https://mcp.crm.example"}
    built = event(use("crm_search", info=info))
    assert built is not None
    [tool] = built.available_tools or []
    [server] = built.available_tool_servers or []
    assert (tool.name, server.name, server.kind) == ("crm_search", "crm", "mcp")


@pytest.mark.parametrize(
    "info",
    [
        {"type": "platform", "tool_type": "web_search_20250305"},
        {"type": "application", "toolset_name": "claude-ai"},
        {"type": "something_new"},
    ],
)
def test_every_other_kind_is_a_builtin_under_its_raw_name(info: dict[str, Any]) -> None:
    built = event(use("web_search", info=info))
    assert built is not None
    [server] = built.available_tool_servers or []
    [tool] = built.available_tools or []
    assert (tool.name, server.name) == ("web_search", "builtin")


def test_parallel_calls_are_one_event_with_each_tool_declared_once() -> None:
    built = event(
        use("Bash", "toolu_a", CLIENT, command="echo a"),
        use("Bash", "toolu_b", CLIENT, command="echo b"),
        use("Read", "toolu_c", CLIENT),
    )
    assert built is not None
    assert [u.tool_use_id for u in built.requested_tool_uses or []] == [
        "toolu_a",
        "toolu_b",
        "toolu_c",
    ]
    assert sorted(t.name or "" for t in built.available_tools or []) == ["Bash", "Read"]
    declared = {t.id for t in built.available_tools or []}
    assert {u.tool_id for u in built.requested_tool_uses or []} <= declared


def test_no_actor_id_means_no_event() -> None:
    assert event(use("Bash"), actor={"type": "user", "id": None}) is None


def test_a_frame_without_tool_uses_means_no_event() -> None:
    assert event() is None


# --- through the HTTP surface ---


class Preflight:
    """The SlashID preflight endpoint: records its bodies, answers its reasons."""

    def __init__(self, *reasons: str) -> None:
        self.reasons = list(reasons)
        self.bodies: list[dict[str, Any]] = []

    def client(self) -> httpx.AsyncClient:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/preflight"):
                self.bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"deny_reasons": self.reasons})
            return httpx.Response(200, json={})

        return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def config(**over: Any) -> Config:
    base: dict[str, Any] = {
        "endpoint": "https://api.slashid.example",
        "push_token": "t",
        "hook_signing_secret": SECRET,
        "platform": "gcp",
        "project_id": "proj",
        "shadow_mode": False,
        "preflight_enabled": True,
    }
    return Config(**(base | over))


async def post(
    sign: Signer, cfg: Config, body: dict[str, Any], net: Preflight, store: Any = None
) -> httpx.Response:
    app = create_app(cfg, store=store, client=net.client())
    raw = json.dumps(body).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/", content=raw, headers=sign(raw, body["request_id"]))


async def test_an_allowed_call_is_sent_to_preflight_as_a_requested_tool(sign: Signer) -> None:
    net = Preflight()
    r = await post(sign, config(), frame(use("Bash", "toolu_a", CLIENT, command="ls")), net)
    assert (r.status_code, r.json()) == (200, {"action": "allow"})
    [sent] = net.bodies
    assert sent["request_id"] == "msg_1-tool_call"
    assert [u["tool_use_id"] for u in sent["requested_tool_uses"]] == ["toolu_a"]
    assert sent["available_tools"][0]["name"] == "Bash"


async def test_a_preflight_denial_denies_the_whole_frame(sign: Signer) -> None:
    net = Preflight("Bash is not allowed")
    body = frame(use("Bash", "toolu_a", CLIENT), use("Read", "toolu_b", CLIENT))
    r = await post(sign, config(), body, net)
    wire = r.json()
    assert wire["action"] == "deny"
    assert "Bash is not allowed" in wire["deny_reason"] and wire["deny_reason"].endswith(RECOVERY)
    assert [len(b["requested_tool_uses"]) for b in net.bodies] == [2]  # one verdict, both calls


async def test_shadow_mode_allows_a_denied_call(sign: Signer) -> None:
    r = await post(sign, config(shadow_mode=True), frame(use("Bash")), Preflight("no"))
    assert r.json() == {"action": "allow"}


async def test_the_capture_marker_in_a_tool_input_denies(sign: Signer) -> None:
    net = Preflight()
    cfg = config(capture_deny_marker="SLASHID_DENY_ME")
    r = await post(sign, cfg, frame(use("Bash", command="echo SLASHID_DENY_ME")), net)
    assert r.json()["action"] == "deny"


async def test_without_preflight_the_call_is_allowed_and_nothing_is_sent(sign: Signer) -> None:
    net = Preflight("no")
    r = await post(sign, config(preflight_enabled=False), frame(use("Bash")), net)
    assert r.json() == {"action": "allow"} and net.bodies == []


async def test_a_call_with_no_actor_id_is_allowed_unsent(sign: Signer) -> None:
    net = Preflight("no")
    r = await post(sign, config(), frame(use("Bash"), actor={"type": "user", "id": None}), net)
    assert r.json() == {"action": "allow"} and net.bodies == []


async def test_a_tool_call_frame_writes_no_record(sign: Signer) -> None:
    store = a_store()
    await post(sign, config(), frame(use("Bash")), Preflight(), store=store)
    assert addresses(store) == set()


async def test_a_frame_that_does_not_parse_as_a_tool_call_is_allowed(sign: Signer) -> None:
    broken = frame(use("Bash")) | {"messages": "not a list"}
    r = await post(sign, config(), broken, Preflight("no"))
    assert r.json() == {"action": "allow"}
