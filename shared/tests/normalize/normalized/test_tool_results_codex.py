"""Shell and view_image reads in extract_tool_result_files."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from pydantic import JsonValue

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.normalized.tool_results import (
    extract_tool_result_files,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedMessage

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(
    tool_name: str,
    tool_input: JsonValue,
    tool_output: JsonValue,
    *,
    include_raw_content: bool = False,
) -> list[AIAccessedFile]:
    messages = [
        NormalizedMessage.model_validate(
            {
                "role": "assistant",
                "content": [
                    {
                        "kind": "tool_use",
                        "tool_use_id": "call_1",
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                    }
                ],
            }
        ),
        NormalizedMessage.model_validate(
            {
                "role": "user",
                "content": [
                    {"kind": "tool_result", "tool_use_id": "call_1", "tool_output": tool_output}
                ],
            }
        ),
    ]
    config = BaseConfig(
        endpoint="http://t", push_token="t", include_raw_content=include_raw_content
    )
    return extract_tool_result_files(messages, config=config)


def _cat(command: str = "cat notes.md") -> dict[str, JsonValue]:
    return {"command": command, "workdir": "/w"}


def _single_sha(files: list[AIAccessedFile], name: str) -> str | None:
    assert len(files) == 1
    assert files[0].name == name
    assert files[0].content_hashes is not None
    return files[0].content_hashes["sha256"]


def test_function_mode_output_strips_header() -> None:
    out = (
        "Chunk ID: 520ca3\nWall time: 0.0000 seconds\nProcess exited with code 0\n"
        "Original token count: 1746\nOutput:\nhello\n"
    )
    assert _single_sha(_files("Bash", _cat(), out), "notes.md") == _sha(b"hello\n")


def test_script_mode_output_takes_json_output() -> None:
    out = [
        {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
        {
            "type": "input_text",
            "text": json.dumps(
                {
                    "chunk_id": "0f99b8",
                    "wall_time_seconds": 0.0000035,
                    "exit_code": 0,
                    "original_token_count": 2,
                    "output": "hello\n",
                }
            ),
        },
    ]
    assert _single_sha(_files("Bash", _cat(), out), "notes.md") == _sha(b"hello\n")


def test_function_mode_failed_read_is_not_hashed() -> None:
    out = (
        "Chunk ID: 520ca3\nWall time: 0.0000 seconds\nProcess exited with code 1\n"
        "Original token count: 9\nOutput:\ncat: notes.md: No such file or directory\n"
    )
    files = _files("Bash", _cat(), out)
    assert [f.content_hashes for f in files] == [None]


def test_script_mode_failed_read_is_not_hashed() -> None:
    out = [
        {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
        {
            "type": "input_text",
            "text": json.dumps(
                {"chunk_id": "0f99b8", "exit_code": 1, "output": "cat: notes.md: No such file\n"}
            ),
        },
    ]
    files = _files("Bash", _cat(), out)
    assert [f.content_hashes for f in files] == [None]


def test_bare_stdout_is_hashed_whole() -> None:
    files = _files("Bash", {"command": "cat /etc/hosts"}, "127.0.0.1 localhost\n")
    assert _single_sha(files, "hosts") == _sha(b"127.0.0.1 localhost\n")


def test_bare_stdout_with_output_line_is_hashed_whole() -> None:
    out = "notes\nOutput:\nmore\n"
    assert _single_sha(_files("Bash", _cat(), out), "notes.md") == _sha(out.encode())


def test_home_relative_read_is_named_by_file_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/root")
    files = _files("Bash", {"command": "cat ~/x"}, "hello\n")
    assert _single_sha(files, "x") == _sha(b"hello\n")


def test_background_command_is_not_a_read() -> None:
    tool_input: JsonValue = {"command": "cat /w/notes.md", "run_in_background": True}
    out = "Command running in background with ID: b1"
    assert _files("Bash", tool_input, out) == []


def test_multi_file_cat_is_not_a_read() -> None:
    assert _files("Bash", _cat("cat a b"), "x") == []


def test_converse_and_anthropic_text_parts() -> None:
    for out in ([{"text": "hello\n"}], [{"type": "text", "text": "hello\n"}]):
        assert _single_sha(_files("Bash", _cat(), out), "notes.md") == _sha(b"hello\n")


def _image_output(mime: str = "application/octet-stream") -> list[JsonValue]:
    url = f"data:{mime};base64," + base64.b64encode(_PNG).decode()
    return [{"type": "input_image", "image_url": url}]


@pytest.mark.parametrize("mime", ["application/octet-stream", "image/png"])
def test_view_image_hashes_decoded_bytes(mime: str) -> None:
    files = _files("view_image", {"path": "/w/img.png"}, _image_output(mime))
    assert _single_sha(files, "img.png") == _sha(_PNG)
    assert files[0].byte_length == len(_PNG)


def test_view_image_never_carries_redacted_content() -> None:
    files = _files("view_image", {"path": "/w/img.png"}, _image_output(), include_raw_content=True)
    assert files[0].redacted_content is None


def test_read_unchanged() -> None:
    files = _files("Read", {"file_path": "/tmp/x.py"}, "     1\thello\n")
    assert _single_sha(files, "x.py") == _sha(b"hello\n")
