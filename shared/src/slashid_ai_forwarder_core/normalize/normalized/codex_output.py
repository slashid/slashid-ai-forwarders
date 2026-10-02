"""Path and content extractors for shell and ``view_image`` reads in
``_READ_TOOLS``, covering Codex's output wrappers and other sources' plain
text."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from ...reads import bash_read_path, get_file_read_by_tool

_HEADER_PREFIX = "Chunk ID: "
_HEADER_END = "\nOutput:\n"
_EXIT_FAILED = re.compile(r"^Process exited with code (?!0$)", re.MULTILINE)


class _ShellInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    workdir: str | None = None
    run_in_background: bool = False


class _InputText(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["input_text"]
    text: str


class _InputImage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["input_image"]
    image_url: str


class _ScriptResult(BaseModel):
    model_config = ConfigDict(extra="ignore")
    chunk_id: str
    exit_code: int | None = None
    output: str


_ScriptParts = TypeAdapter(tuple[_InputText, _InputText])
_Parts = TypeAdapter(list[JsonValue])


def shell_read_path(tool_input: JsonValue) -> str | None:
    """The file a shell call read, resolved against its ``workdir`` when it
    has one. A background call returns only its id, so it reads nothing."""
    try:
        shell = _ShellInput.model_validate(tool_input)
    except ValidationError:
        return None
    if shell.run_in_background:
        return None
    path = bash_read_path(shell.command, shell.workdir)
    return str(path) if path else None


def view_image_path(tool_input: JsonValue) -> str | None:
    path = get_file_read_by_tool("view_image", tool_input, None)
    return str(path) if path else None


def shell_stdout(tool_output: JsonValue) -> JsonValue:
    """The stdout a shell call returned: after Codex's ``Output:`` header, or
    the JSON ``output`` part in script mode; ``None`` when Codex reports a
    non-zero exit; any other shape unchanged."""
    if isinstance(tool_output, str) and tool_output.startswith(_HEADER_PREFIX):
        header, sep, stdout = tool_output.partition(_HEADER_END)
        if not sep:
            return tool_output
        return None if _EXIT_FAILED.search(header) else stdout
    try:
        _, part = _ScriptParts.validate_python(tool_output)
        result = _ScriptResult.model_validate_json(part.text)
    except ValidationError:
        return tool_output
    return result.output if result.exit_code in (0, None) else None


def image_output_bytes(tool_output: JsonValue) -> bytes | None:
    """The decoded bytes of the first ``input_image`` data URL."""
    try:
        parts = _Parts.validate_python(tool_output)
    except ValidationError:
        return None
    for part in parts:
        try:
            return _decode_data_url(_InputImage.model_validate(part).image_url)
        except ValidationError:
            continue
    return None


def _decode_data_url(url: str) -> bytes | None:
    header, sep, payload = url.partition(",")
    if not sep or not header.startswith("data:") or not header.endswith(";base64"):
        return None
    try:
        return base64.b64decode(payload, validate=True)
    except binascii.Error:
        return None
