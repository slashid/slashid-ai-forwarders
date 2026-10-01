from __future__ import annotations

from pathlib import Path

import pytest

from slashid_codex.config import CodexConfig

TOKEN = "t" * 32


def _write(tmp_path: Path, body: str, token: str | None = TOKEN) -> Path:
    token_file = tmp_path / "token"
    if token is not None:
        token_file.write_text(token)
    config = tmp_path / "config.toml"
    config.write_text(f'push_token_file = "{token_file}"\n' + body)
    return config


BASE = 'endpoint = "https://api.slashid.com/"\nuser_id = "user-abc"\n'


def test_loads_toml(tmp_path: Path) -> None:
    cfg = CodexConfig.load(
        _write(tmp_path, BASE + 'verdict_fail_mode = "allow"\ncodex_home = "/x/.codex"\n')
    )
    assert cfg.endpoint == "https://api.slashid.com"
    assert cfg.user_id == "user-abc"
    assert cfg.verdict_fail_mode == "allow"
    assert cfg.codex_home == Path("/x/.codex")
    assert cfg.preflight_timeout_seconds == 4.0
    assert cfg.max_file_bytes == 50 * 1024 * 1024
    assert cfg.daemon_idle_seconds == 600
    assert cfg.codex_bin is None
    assert cfg.dry_run is False


def test_defaults(tmp_path: Path) -> None:
    cfg = CodexConfig.load(_write(tmp_path, BASE))
    assert cfg.verdict_fail_mode == "deny"
    assert cfg.input_scope == "round"
    assert cfg.round_link_depth == 10
    assert cfg.include_raw_content is False
    assert cfg.codex_home == Path.home() / ".codex"


def test_token_read_from_file_and_stripped(tmp_path: Path) -> None:
    cfg = CodexConfig.load(_write(tmp_path, BASE, token=f"  {TOKEN}\n"))
    assert cfg.push_token == TOKEN


def test_environment_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLASHID_ENDPOINT", "https://evil.example")
    monkeypatch.setenv("SLASHID_DRY_RUN", "true")
    monkeypatch.setenv("SLASHID_PUSH_TOKEN", "e" * 40)
    cfg = CodexConfig.load(_write(tmp_path, BASE))
    assert cfg.endpoint == "https://api.slashid.com"
    assert cfg.dry_run is False
    assert cfg.push_token == TOKEN


@pytest.mark.parametrize(
    "endpoint",
    ["http://x", "https://u:p@x", "https://u@x", "https://x?a=1", "https://x#f", "https://", "x"],
)
def test_endpoint_rejected(tmp_path: Path, endpoint: str) -> None:
    with pytest.raises(ValueError):
        CodexConfig.load(_write(tmp_path, f'endpoint = "{endpoint}"\nuser_id = "user-abc"\n'))


@pytest.mark.parametrize("token", ["t" * 31, " " * 40, "t" * 20 + " " + "t" * 20, ""])
def test_short_token_rejected(tmp_path: Path, token: str) -> None:
    with pytest.raises(ValueError):
        CodexConfig.load(_write(tmp_path, BASE, token=token))


def test_missing_token_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        CodexConfig.load(_write(tmp_path, BASE, token=None))


def test_dry_run_keeps_validation(tmp_path: Path) -> None:
    assert CodexConfig.load(_write(tmp_path, BASE + "dry_run = true\n")).dry_run
    with pytest.raises(ValueError):
        CodexConfig.load(_write(tmp_path, BASE + "dry_run = true\n", token="short"))


def test_user_id_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        CodexConfig.load(_write(tmp_path, 'endpoint = "https://x"\n'))


def test_errors_do_not_echo_token(tmp_path: Path) -> None:
    secret = "s3cr3t-but-short"
    with pytest.raises(ValueError) as exc:
        CodexConfig.load(_write(tmp_path, BASE, token=secret))
    assert secret not in str(exc.value)
