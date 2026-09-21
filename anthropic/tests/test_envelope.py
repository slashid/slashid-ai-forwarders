"""Frame → the partial event a pending record is made of."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Literal

from pydantic import BaseModel
from slashid_ai_forwarder_core.events import AIAccessedFile
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.config import Config
from slashid_anthropic_forwarder.hook.envelope import accessed_files_for, attachment_files
from slashid_anthropic_forwarder.hook.frame import PromptFrame
from tests.conftest import SECRET

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# The attested webhook-timestamp of the captured deliveries.
SIGNED_AT = 1789945700
# The provisional key is computed by the store's addressing module, in a
# later chunk, and handed to the builder; nothing here derives it.
ADDRESS = "inv:0123456789abcdef0123456789abcdef"


def load(name: str) -> PromptFrame:
    return PromptFrame.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))


def config(**overrides: Any) -> Config:
    return Config(
        endpoint="https://api.slashid.com",
        push_token="t",
        hook_signing_secret=SECRET,
        **overrides,
    )


class ExpectedFile(BaseModel):
    name: str | None
    sha256: str
    media_type: str | None = None
    byte_length: int | None = None
    provenance: Literal["tool_result", "attachment"] = "tool_result"


def check_files(files: list[AIAccessedFile] | None, expected: list[ExpectedFile]) -> None:
    got = files or []
    assert [(f.name, (f.content_hashes or {}).get("sha256")) for f in got] == [
        (e.name, e.sha256) for e in expected
    ]
    for g, e in zip(got, expected, strict=True):
        assert g.content_hashes is not None and set(g.content_hashes) == {"sha256", "sha1", "md5"}
        assert g.provenance == e.provenance
        if e.media_type is not None:
            assert g.media_type == e.media_type
        if e.byte_length is not None:
            assert g.byte_length == e.byte_length


@yaml_pytest(filename="test_accessed_files_for.yaml")
async def test_accessed_files_for(fixture: str, expected: list[ExpectedFile]) -> None:
    check_files(await accessed_files_for(load(fixture).messages, config=config()), expected)


async def test_a_nameless_attachment_takes_its_name_by_media_type() -> None:
    """The <uploaded_files> block lists pdf, jpeg, txt; the attachment blocks
    run txt, jpeg, pdf. Order cannot pair them, so the nameless PDF claims
    the listed name whose media type matches — and the image, carrying no
    text, produces no entry to name at all."""
    files = await accessed_files_for(load("frame_attachment").messages, config=config())
    assert [f.name for f in files] == ["maria.txt", "guiaSADT.pdf"]


async def test_attachment_text_rides_along_only_under_include_raw_content() -> None:
    messages = load("frame_attachment").messages
    files = attachment_files(messages, config=config(include_raw_content=True))
    # AIAccessedFile is a _WireModel (str_strip_whitespace=True), so the
    # stored text loses the trailing newline; the digest and byte_length
    # in the case table above are over the unstripped bytes.
    assert files[0].redacted_content == "Maria tinha um carneirinho"
    assert attachment_files(messages, config=config())[0].redacted_content is None
