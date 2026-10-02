from __future__ import annotations

from pathlib import Path

from slashid_codex.attachments import Attachment, parse_attachments
from slashid_codex.hooks import UserPromptSubmitHook

HOOKS = Path(__file__).parent / "fixtures" / "hooks"


def _section(*entries: str, request: str = "hi") -> str:
    body = "\n\n".join(entries)
    return f"# Files mentioned by the user:\n\n{body}\n\n## My request:\n{request}"


def test_captured_prompt() -> None:
    hook = UserPromptSubmitHook.model_validate_json(
        (HOOKS / "user_prompt_submit_attachments.json").read_bytes()
    )
    assert parse_attachments(hook.prompt) == [
        Attachment(
            name="Relatório — Março 2026.pdf",
            path="/home/user/Downloads/Relatório — Março 2026.pdf",
            is_image=False,
        ),
        Attachment(
            name="report.pdf", path="/home/user/Downloads/Avó/Cópias/report.pdf", is_image=False
        ),
        Attachment(name="notes.md", path="/home/user/Downloads/notes.md", is_image=False),
        Attachment(
            name="image.png", path="/home/user/Downloads/Avó/Cópias/image.png", is_image=True
        ),
    ]


def test_name_with_colon_space() -> None:
    prompt = _section("## Re: plan.txt: /tmp/Re: plan.txt")
    assert parse_attachments(prompt) == [
        Attachment(name="Re: plan.txt", path="/tmp/Re: plan.txt", is_image=False)
    ]


def test_windows_path() -> None:
    assert parse_attachments(_section(r"## y.txt: C:\x\y.txt")) == [
        Attachment(name="y.txt", path=r"C:\x\y.txt", is_image=False)
    ]


def test_windows_path_forward_slash() -> None:
    assert parse_attachments(_section("## y.txt: C:/x/y.txt")) == [
        Attachment(name="y.txt", path="C:/x/y.txt", is_image=False)
    ]


def test_windows_unc_path() -> None:
    assert parse_attachments(_section(r"## y.txt: \\server\share\y.txt")) == [
        Attachment(name="y.txt", path=r"\\server\share\y.txt", is_image=False)
    ]


def test_windows_rooted_without_drive_not_an_attachment() -> None:
    assert parse_attachments(_section(r"## a.txt: \x\y.txt")) == []


def test_no_section() -> None:
    assert parse_attachments("## a.txt: /tmp/a.txt") == []
    assert parse_attachments("just a prompt") == []


def test_section_not_at_start() -> None:
    assert parse_attachments("please read\n" + _section("## a.txt: /tmp/a.txt")) == []


def test_stops_at_request() -> None:
    prompt = _section("## a.txt: /tmp/a.txt", request="## b.txt: /tmp/b.txt")
    assert [a.path for a in parse_attachments(prompt)] == ["/tmp/a.txt"]


def test_relative_path_not_an_attachment() -> None:
    assert parse_attachments(_section("## a.txt: a.txt")) == []
