from __future__ import annotations

from slashid_ai_forwarder_core.events import AIAccessedFile

from slashid_codex.envelope import drop_shadowed_stubs


def _file(name: str | None, *, hashed: bool = False, ref: str | None = None) -> AIAccessedFile:
    return AIAccessedFile(
        name=name, content_hashes={"sha256": "h"} if hashed else None, file_ref=ref
    )


def test_a_hashed_file_shadows_the_unhashed_entry_of_its_name() -> None:
    stub, full = _file("pix.png"), _file("pix.png", hashed=True)
    assert drop_shadowed_stubs([stub, full]) == [full]
    assert drop_shadowed_stubs([full, stub]) == [full]


def test_a_stub_alone_is_kept() -> None:
    stub = _file("pix.png")
    assert drop_shadowed_stubs([stub, _file("other.png", hashed=True)]) == [
        stub,
        _file("other.png", hashed=True),
    ]


def test_an_entry_with_a_ref_is_never_dropped_and_never_shadows() -> None:
    remote_stub = _file("a.md", ref="google/A")
    remote_full = _file("a.md", hashed=True, ref="google/A")
    local_stub = _file("a.md")
    assert drop_shadowed_stubs([remote_stub, local_stub, remote_full]) == [
        remote_stub,
        local_stub,
        remote_full,
    ]
    local_full = _file("a.md", hashed=True)
    assert drop_shadowed_stubs([remote_stub, local_full]) == [remote_stub, local_full]


def test_nameless_entries_shadow_nothing() -> None:
    nameless_stub, nameless_full = _file(None, ref="google/A"), _file(None, hashed=True)
    assert drop_shadowed_stubs([nameless_stub, nameless_full]) == [nameless_stub, nameless_full]
    plain = _file(None)
    assert drop_shadowed_stubs([plain, nameless_full]) == [plain, nameless_full]
