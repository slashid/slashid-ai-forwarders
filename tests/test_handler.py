"""Smoke test the handler module imports."""

from __future__ import annotations


def test_handler_imports():
    from slashid_bedrock_forwarder import handler  # noqa: F401
