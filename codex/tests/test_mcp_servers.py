from __future__ import annotations

import stat
from pathlib import Path

import pytest
from slashid_ai_forwarder_core.normalize.normalized.tools import resolve_tool

from slashid_codex import mcp_servers
from slashid_codex.mcp_servers import McpServers, find_codex, parse_servers

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_list.json"


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "codex"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_parse_enabled_servers() -> None:
    servers = parse_servers(FIXTURE.read_bytes())
    assert [(s.name, s.kind) for s in servers] == [
        ("node_repl", "mcp"),
        ("cua_repl", "mcp"),
        ("docs", "mcp"),
    ]
    # The id a call to one of its tools declares.
    assert servers[0].id == resolve_tool("mcp__node_repl__js")[1].id
    dumped = "".join(s.model_dump_json() for s in servers)
    for secret in ("*****", "/opt/app", "--stdio", "example.test", "env"):
        assert secret not in dumped


def test_parse_bad_json() -> None:
    assert parse_servers(b"not json") == []
    assert parse_servers(b'{"name": "x"}') == []


def test_find_configured_first(tmp_path: Path) -> None:
    configured = tmp_path / "mine"
    assert (
        find_codex(configured, which=lambda _: "/usr/bin/codex", platform="linux", env={})
        == configured
    )


def test_find_on_path() -> None:
    assert find_codex(None, which=lambda _: "/usr/bin/codex", platform="linux", env={}) == Path(
        "/usr/bin/codex"
    )


@pytest.mark.parametrize(
    ("platform", "env", "expected"),
    [
        ("linux", {}, Path("/usr/lib/chatgpt/resources/codex")),
        ("darwin", {}, Path("/Applications/ChatGPT.app/Contents/Resources/codex")),
        (
            "win32",
            {"LOCALAPPDATA": "C:/Users/u/AppData/Local"},
            Path("C:/Users/u/AppData/Local") / "Programs" / "ChatGPT" / "resources" / "codex.exe",
        ),
        ("win32", {}, None),
    ],
)
def test_find_desktop_bundle(platform: str, env: dict[str, str], expected: Path | None) -> None:
    found = find_codex(
        None, which=lambda _: None, platform=platform, env=env, is_file=lambda _: True
    )
    assert found == expected


def test_find_nothing() -> None:
    assert (
        find_codex(None, which=lambda _: None, platform="linux", env={}, is_file=lambda _: False)
        is None
    )


async def test_lists_and_caches(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    binary = _script(tmp_path, f'echo x >> "{calls}"\ncat "{FIXTURE}"')
    now = [0.0]
    listing = McpServers(binary, clock=lambda: now[0])
    assert [s.name for s in await listing.get()] == ["node_repl", "cua_repl", "docs"]
    now[0] = 599.0
    await listing.get()
    assert calls.read_text().count("x") == 1
    now[0] = 601.0
    await listing.get()
    assert calls.read_text().count("x") == 2


async def test_args(tmp_path: Path) -> None:
    out = tmp_path / "args"
    binary = _script(tmp_path, f'echo "$@" > "{out}"\necho "[]"')
    assert await McpServers(binary).get() == []
    assert out.read_text().strip() == "mcp list --json"


@pytest.mark.parametrize("body", ["exit 1", "echo garbage", f'cat "{FIXTURE}"; exit 2'])
async def test_failures_are_empty(tmp_path: Path, body: str) -> None:
    assert await McpServers(_script(tmp_path, body)).get() == []


async def test_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_servers, "TIMEOUT_S", 0.2)
    assert await McpServers(_script(tmp_path, "sleep 5")).get() == []


async def test_missing_binary(tmp_path: Path) -> None:
    assert await McpServers(tmp_path / "nope").get() == []
    assert await McpServers(None, find=lambda: None).get() == []
