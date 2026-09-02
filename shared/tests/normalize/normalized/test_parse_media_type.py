"""Tests for parse_media_type — IANA-registry-validated MIME parsing with
graceful fallback for unregistered / parameterized / empty inputs."""

from __future__ import annotations

from slashid_ai_forwarder_core.normalize.normalized.media_types import parse_media_type
from slashid_ai_forwarder_core.testing import yaml_pytest


@yaml_pytest()
def test_parse_media_type(raw: str | None, expected: str | None) -> None:
    result = parse_media_type(raw)
    # MimeType is a str subclass; compare via str() so a plain-str `expected`
    # from YAML compares equal to a MimeType instance without extra plumbing.
    assert (str(result) if result is not None else None) == expected
