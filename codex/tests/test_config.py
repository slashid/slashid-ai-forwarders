from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest

from slashid_codex.config import CodexConfig, files_digest

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
    [
        "http://x",
        "https://u:p@x",
        "https://u@x",
        "https://x?a=1",
        "https://x#f",
        "https://",
        "x",
        "https://x:abc",
        "https://x:99999",
    ],
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


def test_unknown_key_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        CodexConfig.load(_write(tmp_path, BASE + "verdict_fail_mod = 'allow'\n"))


def test_relative_paths_resolve_against_config_dir(tmp_path: Path) -> None:
    conf_dir = tmp_path / "etc"
    conf_dir.mkdir()
    (conf_dir / "token").write_text(TOKEN)
    config = conf_dir / "config.toml"
    config.write_text(
        'push_token_file = "token"\ncodex_bin = "bin/codex"\ncodex_home = "home/.codex"\n' + BASE
    )
    cfg = CodexConfig.load(config)
    assert cfg.push_token == TOKEN
    assert cfg.push_token_file == conf_dir / "token"
    assert cfg.codex_bin == conf_dir / "bin/codex"
    assert cfg.codex_home == conf_dir / "home/.codex"


def test_home_relative_paths_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "token").write_text(TOKEN)
    config = tmp_path / "etc.toml"
    config.write_text('push_token_file = "~/token"\ncodex_home = "~/.codex"\n' + BASE)
    cfg = CodexConfig.load(config)
    assert cfg.push_token_file == tmp_path / "token"
    assert cfg.codex_home == tmp_path / ".codex"


def test_constructor_requires_absolute_paths(make_config: Callable[..., CodexConfig]) -> None:
    with pytest.raises(ValueError):
        make_config(codex_home="rel/.codex")
    with pytest.raises(ValueError):
        make_config(codex_bin="codex")


def test_example_config_loads(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "deploy" / "config.example.toml"
    token = tmp_path / "token"
    token.write_text("t" * 32)
    text = example.read_text().replace("/opt/slashid/codex/token", str(token))
    config_path = tmp_path / "config.toml"
    config_path.write_text(text)
    config = CodexConfig.load(config_path)
    assert config.verdict_fail_mode == "deny"
    assert config.push_token == "t" * 32


def test_digest_from_the_bytes_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path, BASE)
    expected = files_digest(path, tmp_path / "token")
    reads: list[str] = []
    for method in ("read_bytes", "read_text"):
        real = getattr(Path, method)

        def spy(self: Path, *args: object, _real=real, **kwargs: object) -> object:
            reads.append(self.name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, spy)
    config, digest = CodexConfig.load_with_digest(path)
    assert digest == expected
    assert config.push_token == TOKEN
    assert sorted(reads) == ["config.toml", "token"]


def test_files_digest(tmp_path: Path) -> None:
    path = _write(tmp_path, BASE)
    token = tmp_path / "token"
    expected = hashlib.sha256(path.read_bytes() + b"\0" + token.read_bytes()).hexdigest()
    assert files_digest(path, token) == expected
    token.write_text("u" * 32)
    assert files_digest(path, token) != expected


def test_files_digest_unreadable(tmp_path: Path) -> None:
    path = _write(tmp_path, BASE)
    with pytest.raises(OSError):
        files_digest(path, tmp_path / "absent")
    with pytest.raises(OSError):
        files_digest(tmp_path / "absent.toml", tmp_path / "token")
