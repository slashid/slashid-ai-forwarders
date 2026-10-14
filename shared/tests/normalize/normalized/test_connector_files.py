"""Drive and Microsoft 365 connector calls → the files they name, by provider ref.

The shapes are those captured from live claude.ai and Codex sessions, with the ids replaced."""

from __future__ import annotations

import base64
import json

import pytest
from pydantic import JsonValue

from slashid_ai_forwarder_core.normalize.normalized.connector_files import (
    ConnectorFile,
    connector_files,
    connector_refs,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _claude(action: str) -> str:
    return f"mcp__claude_ai_Google_Drive__{action}"


def _codex(action: str) -> str:
    return f"mcp__codex_apps__google_drive__{action}"


def _envelope(payload: JsonValue) -> list[JsonValue]:
    """Codex's synthesized result: ``{content, structuredContent, isError}`` as one text part."""
    text = json.dumps({"content": [], "structuredContent": payload, "isError": False})
    return [{"type": "input_text", "text": text}]


def _got(files: list[ConnectorFile]) -> list[tuple[str, str | None, bytes | None]]:
    return [(f.ref, f.name, f.content) for f in files]


def test_claude_download_gives_the_bytes_and_the_title() -> None:
    result = json.dumps(
        {
            "content": _b64(b"# Bread\n"),
            "id": "ID1",
            "mimeType": "text/markdown",
            "title": "banana-bread.md",
        }
    )
    got = connector_files(_claude("download_file_content"), {"fileId": "ID1"}, result)
    assert _got(got) == [("google/ID1", "banana-bread.md", b"# Bread\n")]


def test_a_download_that_is_not_base64_keeps_the_ref() -> None:
    result = json.dumps({"content": "!!!", "id": "ID1", "title": "banana-bread.md"})
    got = connector_files(_claude("download_file_content"), {"fileId": "ID1"}, result)
    assert _got(got) == [("google/ID1", "banana-bread.md", None)]


def test_a_re_rendering_is_never_content() -> None:
    result = json.dumps({"fileContent": "\\# Bread", "title": "banana-bread.md", "viewUrl": "u"})
    got = connector_files(_claude("read_file_content"), {"fileId": "ID1"}, result)
    assert _got(got) == [("google/ID1", "banana-bread.md", None)]


def test_an_oversize_result_still_gives_the_ref() -> None:
    result = "Error: result (127,093 characters) exceeds maximum allowed tokens."
    got = connector_files(_claude("get_file_metadata"), {"fileId": "ID1"}, result)
    assert _got(got) == [("google/ID1", None, None)]


def test_claude_search_lists_every_hit() -> None:
    result = json.dumps({"files": [{"id": "A", "title": "a.md"}, {"id": "B", "title": "b.pdf"}]})
    got = connector_files(_claude("search_files"), {"query": "q"}, result)
    assert _got(got) == [("google/A", "a.md", None), ("google/B", "b.pdf", None)]


def test_codex_fetch_gives_the_bytes_when_asked_for_them() -> None:
    url = "https://drive.google.com/file/d/ID2/view"
    payload = {"id": "ID2", "title": "g.md", "b64_string": _b64(b"x")}
    got = connector_files(_codex("fetch"), {"url": url}, _envelope(payload))
    assert _got(got) == [("google/ID2", "g.md", b"x")]


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        ("https://drive.google.com/file/d/ID2/view?usp=sharing", "google/ID2"),
        ("https://docs.google.com/document/d/ID3/edit?tab=t.0", "google/ID3"),
        ("https://drive.google.com/open?id=ID4", "google/ID4"),
    ],
)
def test_codex_fetch_without_a_result_takes_the_id_from_the_url(url: str, ref: str) -> None:
    assert _got(connector_files(_codex("fetch"), {"url": url}, None)) == [(ref, None, None)]


def test_codex_metadata_and_search() -> None:
    meta = connector_files(
        _codex("get_file_metadata"),
        {"fileId": "ID5", "fields": "id,name"},
        _envelope({"id": "ID5", "title": "m.md"}),
    )
    assert _got(meta) == [("google/ID5", "m.md", None)]
    results = [
        {"id": "A", "title": "a.md", "file_or_folder": "file"},
        {"id": "F", "title": "dir", "file_or_folder": "folder"},
        {"title": "no id"},
    ]
    found = connector_files(_codex("search"), {"query": "q"}, _envelope({"results": results}))
    assert _got(found) == [("google/A", "a.md", None)]


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (_codex("fetch"), {"url": "https://example.com/d/ID6/"}),
        (_codex("fetch"), {"url": 3}),
        (_claude("read_file_content"), {}),
        ("Bash", {"command": "ls"}),
        ("mcp__x__y", {"fileId": "ID1"}),
        ("mcp__claude_ai_Gmail__read", {"fileId": "ID1"}),
    ],
)
def test_nothing_else_gives_a_file(tool: str, args: JsonValue) -> None:
    assert connector_files(tool, args, None) == []


def test_refs_from_the_arguments_alone() -> None:
    got = connector_refs(_claude("download_file_content"), {"fileId": "ID1"})
    assert _got(got) == [("google/ID1", None, None)]
