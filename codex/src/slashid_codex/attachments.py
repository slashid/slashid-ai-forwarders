"""The attachment section Codex puts at the start of a ``UserPromptSubmit``
prompt (spec "Attachments")."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel, ConfigDict

_HEADER = "# Files mentioned by the user:"
_REQUEST = "## My request:"
_IMAGE_MARKER = "Image attachment: true"


def _is_absolute(path: str) -> bool:
    # Either absolute form, independent of the host platform: the daemon
    # and Codex run on the same machine, but tests exercise both here.
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


class Attachment(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    path: str
    is_image: bool = False


def parse_attachments(prompt: str) -> list[Attachment]:
    """Only a section at the start of the prompt counts, up to ``## My request:``."""
    text = prompt.lstrip()
    if not text.startswith(_HEADER):
        return []
    found: list[Attachment] = []
    for line in text[len(_HEADER) :].splitlines():
        if line.startswith(_REQUEST):
            break
        if line.strip() == _IMAGE_MARKER and found:
            found[-1] = found[-1].model_copy(update={"is_image": True})
        elif line.startswith("## ") and (entry := _entry(line[3:])) is not None:
            found.append(entry)
    return found


def _entry(line: str) -> Attachment | None:
    """Split at the last ``": "`` followed by an absolute path."""
    end = len(line)
    while (cut := line.rfind(": ", 0, end)) != -1:
        path = line[cut + 2 :]
        if _is_absolute(path):
            return Attachment(name=line[:cut], path=path)
        end = cut
    return None
