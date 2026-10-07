"""OpenAI Chat Completions image / file parts → AIAccessedFile[]."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.normalize.converse import s3
from slashid_ai_forwarder_core.normalize.openai.chat.attachments import extract_attachments
from slashid_ai_forwarder_core.normalize.openai.chat.normalize import (
    chat_stream_to_normalized_invocation,
    chat_to_normalized_invocation,
)
from slashid_ai_forwarder_core.normalize.openai.chat.schema import (
    ChatCompletion,
    ChatRequest,
    ChatStream,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_CONFIG = BaseConfig(endpoint="http://test", push_token="test")
_STREAM = TypeAdapter(ChatStream)
_PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes"


def _hashes(data: bytes) -> dict[str, str]:
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha1": hashlib.sha1(data).hexdigest(),
        "md5": hashlib.md5(data).hexdigest(),
    }


def _data_url(media_type: str, data: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


def _image(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


def _file(**file: str) -> dict[str, Any]:
    return {"type": "file", "file": file}


def _request(*messages: dict[str, Any]) -> ChatRequest:
    return ChatRequest.model_validate({"messages": list(messages)})


def _user(*parts: dict[str, Any]) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": "look"}, *parts]}


async def _extract(request: ChatRequest, config: BaseConfig = _CONFIG) -> list[AIAccessedFile]:
    return await extract_attachments(request, config=config)


async def test_inline_image_is_hashed_and_named_from_its_media_type() -> None:
    files = await _extract(_request(_user(_image(_data_url("image/png", _PNG)))))
    assert files == [
        AIAccessedFile(
            name="image.png",
            media_type="image/png",
            byte_length=len(_PNG),
            content_hashes=_hashes(_PNG),
            provenance="attachment",
        )
    ]


@pytest.mark.parametrize(
    ("media_type", "name"),
    [("image/jpeg", "image.jpg"), ("image/gif", "image.gif"), ("image/webp", "image.webp")],
)
async def test_inline_image_extension_follows_the_media_type(media_type: str, name: str) -> None:
    files = await _extract(_request(_user(_image(_data_url(media_type, b"x")))))
    assert [f.name for f in files] == [name]


async def test_inline_file_keeps_its_filename_or_gets_one_from_its_media_type() -> None:
    named = _file(filename="secret.pdf", file_data=_data_url("application/pdf", b"%PDF"))
    anonymous = _file(file_data=_data_url("application/pdf", b"%PDF-2"))
    files = await _extract(_request(_user(named, anonymous)))
    assert [(f.name, f.media_type) for f in files] == [
        ("secret.pdf", "application/pdf"),
        ("file.pdf", "application/pdf"),
    ]


async def test_real_fixture_hashes_match_the_decoded_bytes() -> None:
    record = json.loads((_FIXTURES / "openai_chat_file_pdf_data_mil.json").read_text())
    request = ChatRequest.model_validate(record["input"]["inputBodyJson"])
    url = record["input"]["inputBodyJson"]["messages"][0]["content"][1]["file"]["file_data"]
    raw = base64.b64decode(url.partition(",")[2])
    (file,) = await _extract(request)
    assert file.content_hashes == _hashes(raw)
    assert (file.name, file.byte_length) == ("secret.pdf", len(raw))


async def test_only_the_fresh_turn_is_reported() -> None:
    old = _user(_image(_data_url("image/png", b"old")))
    new = _user(_image(_data_url("image/png", b"new")))
    files = await _extract(_request(old, {"role": "assistant", "content": "seen"}, new))
    assert [f.content_hashes for f in files] == [_hashes(b"new")]


async def test_text_attachments_carry_raw_content_only_on_request() -> None:
    part = _file(filename="n.md", file_data=_data_url("text/markdown", b"# hello"))
    request = _request(_user(part, _image(_data_url("image/png", _PNG))))
    (plain, _) = await _extract(request)
    assert plain.redacted_content is None
    config = BaseConfig(endpoint="http://test", push_token="test", include_raw_content=True)
    text, image = await _extract(request, config)
    assert text.redacted_content == "# hello"
    assert image.redacted_content is None


async def test_unreadable_and_foreign_urls_are_skipped() -> None:
    files = await _extract(
        _request(
            _user(
                _image("https://example.com/a.png"),
                _image("data:image/png;base64,!!!not-base64!!!"),
                _file(file_id="file-abc123"),
                {"type": "input_audio", "input_audio": {"data": "x"}},
            )
        )
    )
    assert files == []


def _resolver(**resolved: Any):
    async def resolve(source: dict[str, Any], *, max_content_size: int) -> None:
        del max_content_size
        source.update(resolved)

    return resolve


async def test_s3_image_within_the_size_limit_is_hashed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        s3,
        "_resolve_s3_attachment",
        _resolver(
            _resolved_byte_length=len(_PNG),
            _resolved_bytes=_PNG,
            _resolved_content_type="image/png",
        ),
    )
    files = await _extract(_request(_user(_image("s3://bucket/dir/shot.png"))))
    assert files == [
        AIAccessedFile(
            name="shot.png",
            media_type="image/png",
            byte_length=len(_PNG),
            content_hashes=_hashes(_PNG),
            provenance="attachment",
        )
    ]


async def test_s3_file_over_the_size_limit_has_a_length_but_no_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        s3,
        "_resolve_s3_attachment",
        _resolver(
            _resolved_byte_length=9_999_999, _resolved_head_bytes=b"a", _resolved_tail_bytes=b"z"
        ),
    )
    files = await _extract(_request(_user(_file(file_id="s3://bucket/big.pdf"))))
    assert [(f.name, f.byte_length, f.content_hashes) for f in files] == [
        ("big.pdf", 9_999_999, None)
    ]


async def test_s3_reference_that_cannot_be_read_is_a_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3, "_resolve_s3_attachment", _resolver())
    files = await _extract(
        _request(_user(_image("s3://bucket/gone.png"), _file(file_id="s3://bucket/gone.pdf")))
    )
    assert [(f.name, f.media_type, f.byte_length, f.content_hashes) for f in files] == [
        ("gone.png", "image/png", None, None),
        ("gone.pdf", "application/pdf", None, None),
    ]


async def test_s3_file_prefers_the_given_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3, "_resolve_s3_attachment", _resolver())
    files = await _extract(
        _request(_user(_file(filename="report.pdf", file_id="s3://bucket/obj-123")))
    )
    assert [f.name for f in files] == ["report.pdf"]


def _completion() -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "object": "chat.completion",
            "id": "c",
            "choices": [
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
            ],
        }
    )


async def test_the_normalizer_reports_attachments() -> None:
    request = _request(_user(_image(_data_url("image/png", _PNG))))
    normalized = await chat_to_normalized_invocation(request, _completion(), config=_CONFIG)
    assert [f.name for f in normalized.accessed_files] == ["image.png"]
    chunks = _STREAM.validate_python(
        [
            {
                "id": "c",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
            }
        ]
    )
    streamed = await chat_stream_to_normalized_invocation(request, chunks, config=_CONFIG)
    assert [f.name for f in streamed.accessed_files] == ["image.png"]
