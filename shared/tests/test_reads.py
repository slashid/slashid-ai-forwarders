from pathlib import Path

import pytest

from slashid_ai_forwarder_core.reads import get_file_read_by_tool

W = "/work/dir"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat notes.md", f"{W}/notes.md"),
        ("cat -n /etc/hosts", "/etc/hosts"),
        ('cat "My File.txt"', f"{W}/My File.txt"),
        ("sed -n '1,240p' /home/u/banana-bread.md", "/home/u/banana-bread.md"),
        ("sed -n '$p' a.txt", f"{W}/a.txt"),
        ("head -n 50 notes.md", f"{W}/notes.md"),
        ("tail -c 100 log.txt", f"{W}/log.txt"),
        ("nl ../x.py", "/work/x.py"),
        ("cat a.txt b.txt", None),
        ("cat x.txt | grep foo", None),
        ("cat x && rm x", None),
        ("cat x > y", None),
        ("cat *.md", None),
        ("cat $(echo x)", None),
        ("pdftotext report.pdf -", None),
        ("sed -i 's/a/b/' x", None),
        ("cat 'unterminated", None),
        ("", None),
    ],
)
def test_bash_targets(command: str, expected: str | None) -> None:
    got = get_file_read_by_tool("Bash", {"command": command}, W)
    assert got == (Path(expected) if expected else None)


def test_view_image_path() -> None:
    assert get_file_read_by_tool("view_image", {"path": "img.png", "detail": "high"}, W) == Path(
        f"{W}/img.png"
    )


def test_relative_path_without_workdir_is_none() -> None:
    assert get_file_read_by_tool("Bash", {"command": "cat notes.md"}, None) is None


def test_other_tools_are_none() -> None:
    assert get_file_read_by_tool("apply_patch", {"input": "..."}, W) is None
    assert get_file_read_by_tool("Bash", "not a dict", W) is None
