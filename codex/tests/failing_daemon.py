"""``python -m tests.failing_daemon daemon …``: the daemon with a collector
whose setup fails. Test only."""

from __future__ import annotations

import sys

from slashid_codex import cli, daemon


def _fail(*_args: object) -> None:
    raise OSError("platform unavailable")


if __name__ == "__main__":
    daemon.create_local_platform = _fail  # ty: ignore[invalid-assignment]
    sys.exit(cli.main(sys.argv[1:]))
