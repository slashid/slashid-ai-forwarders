"""Files a Drive or Microsoft 365 connector call names, by the ref the provider knows them by.

A connector returns a re-rendering of a file, not its stored bytes, so a content hash cannot
match it; the provider's own id can. ``connector_files`` reads the call (and its result, when
there is one); ``connector_refs`` reads the call alone, as a hook sees it before it runs.

Tool names are those of Claude Code's claude.ai connectors (``mcp__claude_ai_Google_Drive__*``)
and of Codex's (``mcp__codex_apps__google_drive__*``, as its hook names them).
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

_CLAUDE = re.compile(r"mcp__claude_ai_(Google_Drive|Microsoft_365)__(\w+)")
_CODEX = re.compile(r"mcp__codex_apps__(google_drive|microsoft_sharepoint)__(\w+)")
_DRIVE_HOSTS = frozenset({"drive.google.com", "docs.google.com"})
_DRIVE_ID = re.compile(r"/d/([A-Za-z0-9_-]+)")

Source = Literal["claude", "codex"]
Family = Literal["drive", "m365"]


class ConnectorFile(BaseModel):
    """A file a call named: its ``ref``, its ``name`` and, if the call returned them, its bytes."""

    model_config = ConfigDict(frozen=True)

    ref: str
    name: str | None = None
    content: bytes | None = None


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _FileIdArgs(_Lenient):
    fileId: str


class _UrlArgs(_Lenient):
    url: str


class _Envelope(_Lenient):
    """Codex's result: ``{content, structuredContent, isError}``."""

    structuredContent: JsonValue = None


class _DriveFile(_Lenient):
    id: str | None = None
    title: str | None = None
    file_name: str | None = None
    file_or_folder: str | None = None
    content: str | None = None
    b64_string: str | None = None


class _DriveFiles(_Lenient):
    """claude.ai's ``files`` and Codex's ``results``."""

    files: list[_DriveFile] = []
    results: list[_DriveFile] = []


def connector_files(
    tool_name: str, tool_input: JsonValue, tool_output: JsonValue
) -> list[ConnectorFile]:
    """The files of one connector call, ``[]`` for any other call. ``tool_output`` is ``None``
    when there is no result."""
    if match := _CLAUDE.fullmatch(tool_name):
        source: Source = "claude"
        family: Family = "drive" if match.group(1) == "Google_Drive" else "m365"
    elif match := _CODEX.fullmatch(tool_name):
        source = "codex"
        family = "drive" if match.group(1) == "google_drive" else "m365"
    else:
        return []
    handler = _HANDLERS.get((source, family))
    return handler(match.group(2), tool_input, tool_output) if handler else []


def connector_refs(tool_name: str, tool_input: JsonValue) -> list[ConnectorFile]:
    """What the arguments alone name: the refs, with no name from a result and no content."""
    return connector_files(tool_name, tool_input, None)


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------


def _args[M: BaseModel](model: type[M], tool_input: JsonValue) -> M | None:
    try:
        return model.model_validate(tool_input)
    except ValidationError:
        return None


def _texts(tool_output: JsonValue) -> list[str]:
    """A result's text: a string is one text, a list of ``{text}`` parts gives each part's."""
    if isinstance(tool_output, str):
        return [tool_output]
    if isinstance(tool_output, list):
        return [
            p["text"] for p in tool_output if isinstance(p, dict) and isinstance(p.get("text"), str)
        ]
    return []


def _json[M: BaseModel](model: type[M], text: str) -> M | None:
    try:
        return model.model_validate_json(text)
    except ValidationError:
        return None


def _result[M: BaseModel](model: type[M], tool_output: JsonValue) -> M | None:
    """claude.ai: the first text is the tool's JSON."""
    texts = _texts(tool_output)
    return _json(model, texts[0]) if texts else None


def _payload[M: BaseModel](model: type[M], tool_output: JsonValue) -> M | None:
    """Codex: the tool's JSON is the envelope's ``structuredContent``."""
    texts = _texts(tool_output)
    envelope = _json(_Envelope, texts[0]) if texts else None
    if envelope is None:
        return None
    try:
        return model.model_validate(envelope.structuredContent)
    except ValidationError:
        return None


def _bytes(b64: str | None) -> bytes | None:
    if not b64:
        return None
    try:
        return base64.b64decode(b64, validate=True)
    except binascii.Error:
        return None


# ----------------------------------------------------------------------------
# Google Drive
# ----------------------------------------------------------------------------


def _google(file_id: str) -> str:
    return f"google/{file_id}"


def _drive_id_of(url: str) -> str | None:
    parts = urlparse(url)
    if parts.hostname not in _DRIVE_HOSTS:
        return None
    if match := _DRIVE_ID.search(parts.path):
        return match.group(1)
    ids = parse_qs(parts.query).get("id")
    return ids[0] if ids else None


def _claude_drive(
    action: str, tool_input: JsonValue, tool_output: JsonValue
) -> list[ConnectorFile]:
    if action == "search_files":
        found = _result(_DriveFiles, tool_output)
        return [
            ConnectorFile(ref=_google(f.id), name=f.title)
            for f in (found.files if found else [])
            if f.id
        ]
    if action not in {"read_file_content", "get_file_metadata", "download_file_content"}:
        return []
    args = _args(_FileIdArgs, tool_input)
    if args is None:
        return []
    file = _result(_DriveFile, tool_output)
    content = _bytes(file.content) if file and action == "download_file_content" else None
    return [
        ConnectorFile(ref=_google(args.fileId), name=file.title if file else None, content=content)
    ]


def _codex_drive(action: str, tool_input: JsonValue, tool_output: JsonValue) -> list[ConnectorFile]:
    if action == "search":
        found = _payload(_DriveFiles, tool_output)
        return [
            ConnectorFile(ref=_google(f.id), name=f.title)
            for f in (found.results if found else [])
            if f.id and f.file_or_folder != "folder"
        ]
    file = _payload(_DriveFile, tool_output)
    if action == "fetch":
        url = _args(_UrlArgs, tool_input)
        file_id = (file.id if file else None) or (_drive_id_of(url.url) if url else None)
        if not file_id:
            return []
        name = (file.title or file.file_name) if file else None
        return [
            ConnectorFile(
                ref=_google(file_id), name=name, content=_bytes(file.b64_string) if file else None
            )
        ]
    if action == "get_file_metadata":
        args = _args(_FileIdArgs, tool_input)
        if args is None:
            return []
        return [ConnectorFile(ref=_google(args.fileId), name=file.title if file else None)]
    return []


def _no_m365(action: str, tool_input: JsonValue, tool_output: JsonValue) -> list[ConnectorFile]:
    return []


_Handler = Callable[[str, JsonValue, JsonValue], list[ConnectorFile]]
_HANDLERS: dict[tuple[Source, Family], _Handler] = {
    ("claude", "drive"): _claude_drive,
    ("codex", "drive"): _codex_drive,
    ("claude", "m365"): _no_m365,
    ("codex", "m365"): _no_m365,
}
