"""``python -m tests.e2e_daemon daemon …``: the daemon with a config that
accepts a plain ``http://`` endpoint, for the stub SlashID. Test only."""

from __future__ import annotations

import sys

from pydantic import field_validator

from slashid_codex import cli, daemon
from slashid_codex.config import CodexConfig


class PlainHttpConfig(CodexConfig):
    @field_validator("endpoint")
    @classmethod
    def _https_origin(cls, v: str) -> str:
        return v


if __name__ == "__main__":
    daemon.CodexConfig = PlainHttpConfig  # ty: ignore[invalid-assignment]
    sys.exit(cli.main(sys.argv[1:]))
