from pathlib import Path

import pytest

from slashid_ai_forwarder_core.reads import (
    bash_read_path,
    bash_read_paths,
    get_file_read_by_tool,
    get_files_read_by_tool,
)

W = "/work/dir"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat notes.md", f"{W}/notes.md"),
        ("cat -n /etc/hosts", "/etc/hosts"),
        ('cat "My File.txt"', f"{W}/My File.txt"),
        ("sed -n '1,240p' /home/u/banana-bread.md", "/home/u/banana-bread.md"),
        ("sed -n '$p' a.txt", f"{W}/a.txt"),
        ("sed -n '1,$p' a.txt", f"{W}/a.txt"),
        ("cat ~/x", "~/x"),
        ("cat ~", "~"),
        ("nl -b a x.py", f"{W}/x.py"),
        ("nl -w 3 -s : x.py", f"{W}/x.py"),
        ("tail -s 5 -f log.txt", f"{W}/log.txt"),
        ("head -n 50 notes.md", f"{W}/notes.md"),
        ("tail -c 100 log.txt", f"{W}/log.txt"),
        ("nl ../x.py", "/work/x.py"),
        ("cat x.txt | grep foo", None),
        ("cat x && rm x", None),
        ("cat x > y", None),
        ("cat *.md", None),
        ("cat $(echo x)", None),
        ("pdftotext report.pdf -", None),
        ("sed -i 's/a/b/' x", None),
        ("cat 'unterminated", None),
        ("", None),
        ("cat $HOME/.ssh/id_rsa", None),
        ("cat ${X}", None),
        ("cat $'\\x2fetc'", None),
        ("cat {secret,}", None),
        ("cat x\ncat y", None),
        ("cat x\rcat y", None),
        ("cat 'a\nb'", None),
        ('sed -n "$p" a.txt', None),
        ("sed -n $p a.txt", None),
        ("sed -n '1p' $f", None),
        ("cat '~/x'", None),
        ('cat "~/x"', None),
        ("cat \\~/x", None),
        ("cat a=~/x", None),
        ("cat ~root/x", None),
        ("nl -w 3", None),
        ("nl -v 10", None),
        ("tail -f -s 5", None),
        ("cat -", None),
        ("sed -n '1p' -", None),
        ("cat (x)", None),
        ("cat x^y", None),
        ("cat =ls", None),
        ("cat a.txt b.txt", None),
        ("sed -n '1,5p' a.txt b.txt", None),
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


def test_expand_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/u")
    assert get_file_read_by_tool("Bash", {"command": "cat ~/x"}, W, expand_home=True) == Path(
        "/home/u/x"
    )
    assert bash_read_path("cat ~", W, expand_home=True) == Path("/home/u")
    assert bash_read_path("cat ~root/x", W, expand_home=True) is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat a.txt b.txt c.txt", ["a.txt", "b.txt", "c.txt"]),
        ("cat a.txt /etc/hosts", ["a.txt", "/etc/hosts"]),
        ("cat -n a.txt b.txt", ["a.txt", "b.txt"]),
        ("head -n 2 a.txt b.txt", ["a.txt", "b.txt"]),
        ("tail -s 5 a.txt b.txt", ["a.txt", "b.txt"]),
        ("nl -w 3 a.txt b.txt", ["a.txt", "b.txt"]),
        ("sed -n '1,5p' a.txt b.txt", ["a.txt", "b.txt"]),
        ("cat a.txt a.txt b.txt", ["a.txt", "b.txt"]),
        ("cat a.txt - b.txt", ["a.txt", "b.txt"]),
        ('cat "My File.txt" b.txt', ["My File.txt", "b.txt"]),
        ("cat a.txt", ["a.txt"]),
        ("cat -", []),
        ("cat a.txt | head -1", []),
        ("cat a.txt *.md", []),
        ("cat a.txt $F", []),
        ("cat a.txt; cat b.txt", []),
        ("grep x a.txt b.txt", []),
        ("cat", []),
    ],
)
def test_bash_read_paths(command: str, expected: list[str]) -> None:
    got = get_files_read_by_tool("Bash", {"command": command}, W)
    assert got == [Path(p) if p.startswith("/") else Path(W) / p for p in expected]


def test_several_files_are_not_a_single_read() -> None:
    assert bash_read_path("cat a.txt b.txt", W) is None
    # Standard input is a second source, so the output is not that file.
    assert bash_read_path("cat a.txt -", W) is None


def test_tilde_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/u")
    assert bash_read_paths("cat ~/a ~/b", W, expand_home=True) == [
        Path("/home/u/a"),
        Path("/home/u/b"),
    ]
    assert bash_read_paths("cat ~/a ~/b", W) == [Path("~/a"), Path("~/b")]
    # A word that does not resolve is left out; the rest are still reads.
    assert bash_read_paths("cat ~/a ~root/x", W, expand_home=True) == [Path("/home/u/a")]
    assert bash_read_paths("cat ~/a '~/b'", W, expand_home=True) == []
    assert bash_read_paths("cat ~/a b~", W, expand_home=True) == []


def test_other_tools_read_one_file_or_none() -> None:
    assert get_files_read_by_tool("view_image", {"path": "img.png"}, W) == [Path(f"{W}/img.png")]
    assert get_files_read_by_tool("apply_patch", {"input": "..."}, W) == []
    assert get_files_read_by_tool("Bash", "not a dict", W) == []
