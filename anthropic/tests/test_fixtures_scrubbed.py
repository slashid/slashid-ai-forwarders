"""Guard: no real identifier may reach a committed fixture.

The compliance fixtures are recordings of a live tenant. Identifiers hide
inside base64 too — a ``clls_`` session id decodes to JSON carrying the
organization, project and session uuids — so plain text matching is not
enough on its own.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "compliance"

# Values that exist in the real tenant and must never appear.
FORBIDDEN = (
    "2fe4f004",  # the working session this was recorded from
    "34936bc5",  # organization uuid
    "4d621dc4",  # project uuid
    "/home/paulo",
    "paulo",
    "costa.nom.br",
)
PLACEHOLDER_PREFIXES = ("0000000", "1111", "2222")
B64ISH = re.compile(r"[A-Za-z0-9_-]{24,}")
CLLS = re.compile(r"clls_[A-Za-z0-9_-]+")


def _files() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def test_fixtures_exist() -> None:
    assert _files(), f"no compliance fixtures under {FIXTURES}"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_no_forbidden_plaintext(path: Path) -> None:
    text = path.read_text()
    found = [token for token in FORBIDDEN if token in text]
    assert not found, f"{path.name} leaks {found}"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_session_ids_decode_to_placeholders(path: Path) -> None:
    for sid in set(CLLS.findall(path.read_text())):
        raw = sid[len("clls_") :]
        decoded = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        for key, value in decoded.items():
            if not isinstance(value, str):
                continue
            assert value.startswith(PLACEHOLDER_PREFIXES), (
                f"{path.name}: clls_ id carries a real {key}: {value}"
            )


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_nothing_forbidden_hides_in_base64(path: Path) -> None:
    for blob in set(B64ISH.findall(path.read_text())):
        try:
            decoded = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)).decode()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        found = [token for token in FORBIDDEN if token in decoded]
        assert not found, f"{path.name}: base64 blob decodes to {found}"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_every_fixture_is_a_recorded_exchange(path: Path) -> None:
    body = json.loads(path.read_text())
    assert "request" in body or "cases" in body, f"{path.name} is not a recorded call"
