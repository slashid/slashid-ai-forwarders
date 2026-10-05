"""``AIAccessedFile.name`` is a file name, never a path: nothing that builds one
can put a directory on the wire."""

from __future__ import annotations

import pytest

from slashid_ai_forwarder_core.events import AIAccessedFile


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("/home/user/project/notes.md", "notes.md"),
        ("notes.md", "notes.md"),
        ("~/docs/report.pdf", "report.pdf"),
        ("../up/one.txt", "one.txt"),
        (r"C:\Users\x\Downloads\y.txt", "y.txt"),
        ("C:/Users/x/y.txt", "y.txt"),
        (r"\\server\share\z.txt", "z.txt"),
        ("s3://bucket/prefix/doc.pdf", "doc.pdf"),
        ("/home/user/Relatório — Março.pdf", "Relatório — Março.pdf"),
        ("/home/user/dir/", "dir"),
        ("/", None),
        ("", None),
        (None, None),
    ],
)
def test_name_is_reduced_to_the_file_name(given: str | None, sent: str | None) -> None:
    assert AIAccessedFile(name=given).name == sent


def test_the_wire_body_carries_no_directory() -> None:
    file = AIAccessedFile(name="/home/user/secret-project/plan.md", provenance="attachment")
    assert file.model_dump(mode="json", exclude_none=True) == {
        "name": "plan.md",
        "provenance": "attachment",
    }
    assert "secret-project" not in file.model_dump_json()
