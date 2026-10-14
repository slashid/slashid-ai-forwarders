"""Drive and Microsoft 365 connector calls → the files they name, by provider ref.

The shapes are those captured from live claude.ai and Codex sessions, with the ids replaced."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from pydantic import JsonValue

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize.normalized.connector_files import (
    ConnectorFile,
    connector_files,
    connector_refs,
)
from slashid_ai_forwarder_core.normalize.normalized.tool_results import (
    connector_stub,
    extract_tool_result_files,
)
from slashid_ai_forwarder_core.normalize.normalized.types import (
    NormalizedContent,
    NormalizedMessage,
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


DRIVE = "b!drive"
ITEM = "01LZRYC77BC23OVQVCBFFKBLBQDVHL2MRB"
GUID = "ab12cd34-0000-0000-0000-000000000001"
GRAPH = f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/items/{ITEM}"
SOURCEDOC = f"https://t.sharepoint.com/:w:/r/sites/S/_layouts/15/Doc.aspx?sourcedoc=%7B{GUID}%7D&file=a.docx"


def _m365(action: str) -> str:
    return f"mcp__claude_ai_Microsoft_365__{action}"


def _sp(action: str) -> str:
    return f"mcp__codex_apps__microsoft_sharepoint__{action}"


@pytest.mark.parametrize(
    ("uri", "ref", "name"),
    [
        (f"file:///{DRIVE}/{ITEM}", f"m365/item/{DRIVE}/{ITEM}", None),
        (
            f"file:///{DRIVE}/Recipes/banana-bread.md",
            f"m365/path/{DRIVE}/Recipes/banana-bread.md",
            "banana-bread.md",
        ),
        (
            f"file:///{DRIVE}/Recipes/Pipoca%20%25%23.txt",
            f"m365/path/{DRIVE}/Recipes/Pipoca %#.txt",
            "Pipoca %#.txt",
        ),
        (f"file:///{DRIVE}/readme.md", f"m365/path/{DRIVE}/readme.md", "readme.md"),
    ],
)
def test_read_resource_names_an_item_or_a_path(uri: str, ref: str, name: str | None) -> None:
    got = connector_files(_m365("read_resource"), {"uri": uri}, "the text")
    assert _got(got) == [(ref, name, None)]


@pytest.mark.parametrize(
    ("args", "output"),
    [
        ({"uri": "page:///x"}, "t"),
        ({"uri": "mail:///messages/1"}, "t"),
        ({"uri": "file:///"}, "t"),
        ({"uri": f"file:///{DRIVE}"}, "t"),
        ({"uri": 3}, "t"),
        ({}, "t"),
        # A folder: the result lists its children.
        ({"uri": f"file:///{DRIVE}/Recipes"}, f"a.md (file, 12 bytes) file:///{DRIVE}/{ITEM}"),
    ],
)
def test_read_resource_of_anything_else_is_nothing(args: JsonValue, output: str) -> None:
    assert connector_files(_m365("read_resource"), args, output) == []


def test_folder_search_gives_an_item_per_hit() -> None:
    hits = [
        {"uri": "u1", "driveId": DRIVE, "id": ITEM, "name": "a.md"},
        {"uri": "u2", "driveId": DRIVE, "id": "01OTHER", "name": "b.md"},
        {"uri": "u3", "name": "no ids"},
    ]
    output = [{"type": "text", "text": json.dumps(hit)} for hit in hits]
    got = connector_files(_m365("sharepoint_folder_search"), {"name": "n"}, output)
    assert _got(got) == [
        (f"m365/item/{DRIVE}/{ITEM}", "a.md", None),
        (f"m365/item/{DRIVE}/01OTHER", "b.md", None),
    ]


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        (GRAPH, f"m365/item/{DRIVE}/{ITEM}"),
        (SOURCEDOC, f"m365/guid/{GUID.upper()}"),
        ("https://t.sharepoint.com/sites/S/Shared%20Documents/a.md", None),
        ("https://example.com/drives/d/items/i", None),
    ],
)
def test_codex_sharepoint_fetch_takes_the_ref_from_the_url(url: str, ref: str | None) -> None:
    got = connector_files(_sp("fetch"), {"url": url}, None)
    assert _got(got) == ([(ref, None, None)] if ref else [])


def test_the_item_beats_the_guid_when_a_fetch_has_both() -> None:
    payload = {"title": "a.docx", "document_id": GRAPH, "web_link": SOURCEDOC}
    got = connector_files(_sp("fetch"), {"url": SOURCEDOC}, _envelope(payload))
    assert _got(got) == [(f"m365/item/{DRIVE}/{ITEM}", "a.docx", None)]


def test_codex_sharepoint_search_and_get_drive_item() -> None:
    results = [
        {"title": "a.txt", "drive_id": DRIVE, "item_id": ITEM},
        {"title": "no ids"},
    ]
    found = connector_files(_sp("search"), {"query": "q"}, _envelope({"results": results}))
    assert _got(found) == [(f"m365/item/{DRIVE}/{ITEM}", "a.txt", None)]
    item = connector_files(
        _sp("get_drive_item"),
        {"drive_id": DRIVE, "item_id": ITEM},
        _envelope({"name": "b.md"}),
    )
    assert _got(item) == [(f"m365/item/{DRIVE}/{ITEM}", "b.md", None)]


# ----------------------------------------------------------------------------
# Through the extractor
# ----------------------------------------------------------------------------


def _exchange(
    tool: str, args: JsonValue, output: JsonValue, *, is_error: bool = False
) -> list[NormalizedMessage]:
    use = NormalizedContent(kind="tool_use", tool_use_id="t1", tool_name=tool, tool_input=args)
    result = NormalizedContent(
        kind="tool_result", tool_use_id="t1", tool_output=output, tool_is_error=is_error
    )
    return [
        NormalizedMessage(role="assistant", content=[use]),
        NormalizedMessage(role="user", content=[result]),
    ]


def _download(content: str) -> str:
    return json.dumps({"content": content, "id": "ID1", "title": "banana-bread.md"})


CONFIG = BaseConfig(endpoint="http://test", push_token="test")


def test_a_download_becomes_a_hashed_file_with_its_ref() -> None:
    messages = _exchange(
        _claude("download_file_content"), {"fileId": "ID1"}, _download(_b64(b"# Bread\n"))
    )
    [file] = extract_tool_result_files(messages, config=CONFIG)
    assert (file.name, file.file_ref, file.media_type) == (
        "banana-bread.md",
        "google/ID1",
        "text/markdown",
    )
    assert file.provenance == "tool_result"
    assert file.byte_length == len(b"# Bread\n")
    assert file.content_hashes is not None
    assert file.content_hashes["sha256"] == hashlib.sha256(b"# Bread\n").hexdigest()
    assert file.redacted_content is None


def test_other_reads_have_a_ref_and_no_hash() -> None:
    messages = _exchange(
        _claude("read_file_content"), {"fileId": "ID1"}, json.dumps({"title": "banana-bread.md"})
    )
    [file] = extract_tool_result_files(messages, config=CONFIG)
    assert (file.file_ref, file.content_hashes) == ("google/ID1", None)
    bad = _exchange(_claude("download_file_content"), {"fileId": "ID1"}, _download("!!!"))
    [stub] = extract_tool_result_files(bad, config=CONFIG)
    assert (stub.file_ref, stub.content_hashes) == ("google/ID1", None)


def test_a_failed_call_names_nothing() -> None:
    messages = _exchange(
        _claude("download_file_content"), {"fileId": "ID1"}, "denied", is_error=True
    )
    assert extract_tool_result_files(messages, config=CONFIG) == []


def test_an_earlier_result_was_already_reported() -> None:
    messages = _exchange(_claude("get_file_metadata"), {"fileId": "ID1"}, "{}")
    messages.append(NormalizedMessage(role="assistant", content=[_text("done")]))
    assert extract_tool_result_files(messages, config=CONFIG) == []


def _text(text: str) -> NormalizedContent:
    return NormalizedContent(kind="text", text=text)


def test_a_stub_is_the_ref_without_content() -> None:
    stub = connector_stub(ConnectorFile(ref="google/ID1", name="a.md"))
    assert (stub.name, stub.file_ref, stub.media_type) == ("a.md", "google/ID1", "text/markdown")
    assert (stub.provenance, stub.content_hashes) == ("tool_result", None)
    assert connector_stub(ConnectorFile(ref="google/ID2")).name is None


def test_a_non_ascii_download_keeps_the_ref() -> None:
    result = json.dumps({"content": "é", "id": "ID1", "title": "a.pdf"})
    assert _got(connector_files(_claude("download_file_content"), {"fileId": "ID1"}, result)) == [
        ("google/ID1", "a.pdf", None)
    ]
    envelope = _envelope({"id": "ID2", "b64_string": "é"})
    got = connector_files(
        _codex("fetch"), {"url": "https://drive.google.com/file/d/ID2/"}, envelope
    )
    assert _got(got) == [("google/ID2", None, None)]


def test_a_published_link_is_not_a_file_id() -> None:
    url = "https://docs.google.com/document/d/e/2PACX-1vT/pub"
    assert connector_files(_codex("fetch"), {"url": url}, None) == []


def test_claude_search_leaves_folders_out() -> None:
    files = [
        {"id": "A", "title": "a.md", "mimeType": "text/markdown"},
        {"id": "F", "title": "dir", "mimeType": "application/vnd.google-apps.folder"},
    ]
    got = connector_files(_claude("search_files"), {"query": "q"}, json.dumps({"files": files}))
    assert _got(got) == [("google/A", "a.md", None)]


def test_a_result_that_is_not_text_parts_names_nothing() -> None:
    assert connector_files(_claude("search_files"), {"query": "q"}, [1, "x"]) == []
    assert connector_files(_claude("search_files"), {"query": "q"}, {"a": 1}) == []
