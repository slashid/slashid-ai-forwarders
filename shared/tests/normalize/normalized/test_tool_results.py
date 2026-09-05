"""YAML-driven tests for extract_tool_result_files."""

from __future__ import annotations

import hashlib

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.normalized.tool_results import (
    extract_tool_result_files,
)
from slashid_ai_forwarder_core.normalize.normalized.types import NormalizedMessage
from slashid_ai_forwarder_core.testing import yaml_pytest


def _config(*, include_raw_content: bool = False, max_content_size: int = 100_000) -> BaseConfig:
    return BaseConfig(
        endpoint="http://test",
        push_token="test",
        include_raw_content=include_raw_content,
        max_content_size=max_content_size,
    )


@yaml_pytest(filename="test_tool_results.yaml")
def test_extract_tool_result_files(
    messages: list[NormalizedMessage],
    expected: list[AIAccessedFile],
) -> None:
    """Compare only name/byte_length/media_type — content_hashes are derived
    from the bytes and asserted separately for the non-error cases."""
    out = extract_tool_result_files(messages, config=_config())
    assert len(out) == len(expected)
    for got, want in zip(out, expected, strict=True):
        assert got.name == want.name
        assert got.byte_length == want.byte_length
        assert got.media_type == want.media_type
        if want.byte_length is None:
            assert got.content_hashes is None
        else:
            assert got.content_hashes is not None
            assert "sha256" in got.content_hashes


def test_read_cat_n_hash_matches_stripped_bytes() -> None:
    """The Read cleanup strips cat-n prefixes before hashing."""
    msg_asst = NormalizedMessage.model_validate(
        {
            "role": "assistant",
            "content": [
                {
                    "kind": "tool_use",
                    "tool_use_id": "tu_1",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/x.py"},
                    "tool_is_error": False,
                }
            ],
        }
    )
    msg_user = NormalizedMessage.model_validate(
        {
            "role": "user",
            "content": [
                {
                    "kind": "tool_result",
                    "tool_use_id": "tu_1",
                    "tool_output": "     1\thello\n",
                    "tool_is_error": False,
                }
            ],
        }
    )
    out = extract_tool_result_files([msg_asst, msg_user], config=_config())
    assert len(out) == 1
    expected = hashlib.sha256(b"hello\n").hexdigest()
    assert out[0].content_hashes is not None
    assert out[0].content_hashes["sha256"] == expected


def test_extract_tool_result_files_include_raw_populates_redacted() -> None:
    msg_asst = NormalizedMessage.model_validate(
        {
            "role": "assistant",
            "content": [
                {
                    "kind": "tool_use",
                    "tool_use_id": "tu_1",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/tmp/x"},
                    "tool_is_error": False,
                }
            ],
        }
    )
    msg_user = NormalizedMessage.model_validate(
        {
            "role": "user",
            "content": [
                {
                    "kind": "tool_result",
                    "tool_use_id": "tu_1",
                    "tool_output": "secret",
                    "tool_is_error": False,
                }
            ],
        }
    )
    out = extract_tool_result_files([msg_asst, msg_user], config=_config(include_raw_content=True))
    assert out[0].redacted_content == "secret"
