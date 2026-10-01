"""``CodexConfig``: the MDM-installed TOML file named by ``--config``.

Environment variables are not read: hooks inherit the user's environment,
which must not redirect events or swap the token.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from slashid_ai_forwarder_core.config_base import BaseConfig
from slashid_ai_forwarder_core.normalize._base import _LenientModel

MIN_TOKEN_CHARS = 32
_PATH_KEYS = ("push_token_file", "codex_bin", "codex_home")


class _TokenFileRef(_LenientModel):
    push_token_file: Path


class CodexConfig(BaseConfig):
    model_config = SettingsConfigDict(hide_input_in_errors=True, extra="forbid")

    push_token_file: Path
    # The ChatGPT workspace user (`user-…`) events are attributed to.
    user_id: str = Field(..., min_length=1)
    verdict_fail_mode: Literal["allow", "deny"] = "deny"
    preflight_timeout_seconds: float = 4.0
    max_file_bytes: int = 50 * 1024 * 1024
    codex_bin: Path | None = None
    codex_home: Path = Field(default_factory=lambda: Path.home() / ".codex")
    daemon_idle_seconds: int = 600
    # Dev only: preflight and push log what they would send and succeed.
    dry_run: bool = False

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)

    @classmethod
    def load(cls, path: Path) -> Self:
        """Relative paths resolve against the file's directory."""
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for key in _PATH_KEYS:
            if isinstance(value := data.get(key), str):
                data[key] = str(path.parent.absolute() / Path(value).expanduser())
        return cls(**data)

    @model_validator(mode="before")
    @classmethod
    def _read_token_file(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        try:
            token_file = _TokenFileRef.model_validate(data).push_token_file
        except ValidationError:
            return data
        try:
            token = token_file.expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"cannot read push_token_file {token_file}: {exc.strerror}") from None
        return {**data, "push_token": token}

    @field_validator(*_PATH_KEYS, mode="before")
    @classmethod
    def _absolute(cls, v: object) -> object:
        if isinstance(v, str | Path):
            v = Path(v).expanduser()
            if not v.is_absolute():
                raise ValueError("path must be absolute")
        return v

    @field_validator("endpoint")
    @classmethod
    def _https_origin(cls, v: str) -> str:
        parts = urlsplit(v)
        try:
            _ = parts.port
        except ValueError:
            raise ValueError("endpoint port must be a number from 0 to 65535") from None
        if (
            parts.scheme != "https"
            or not parts.hostname
            or "@" in parts.netloc
            or "?" in v
            or "#" in v
        ):
            raise ValueError("endpoint must be https:// without userinfo, query or fragment")
        return v

    @field_validator("push_token")
    @classmethod
    def _token_shape(cls, v: str) -> str:
        if len(v) < MIN_TOKEN_CHARS or any(c.isspace() for c in v):
            raise ValueError(
                f"push token must be at least {MIN_TOKEN_CHARS} non-whitespace characters"
            )
        return v
