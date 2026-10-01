"""The file a tool call is about to read, when the call is simple enough to tell.

Shared by Codex's ``PreToolUse`` preflight and by ``_READ_TOOLS``. ``None``
means "not a recognised single-file read", never an error.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

# Characters that make a shell command more than one plain read, or make bash
# expand a word into a path other than the literal one. ``$`` is checked apart
# since a single-quoted sed range may use it.
_SHELL_META = re.compile(r"[|;&<>`*?\[{}\n\r]")
_SED_RANGE = re.compile(r"[0-9$]+(,[0-9$]+)?p")
# Options that take a value, per reader.
_VALUE_OPTIONS = {"cat": set(), "nl": set(), "head": {"-n", "-c"}, "tail": {"-n", "-c"}}


class _Bash(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str


class _ViewImage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


def get_file_read_by_tool(
    tool_name: str, tool_input: JsonValue, workdir: str | None
) -> Path | None:
    """``view_image`` → its ``path``; ``Bash`` → the one path of a plain
    ``cat``/``head``/``tail``/``nl`` or ``sed -n '<range>p'``. Relative paths
    resolve against ``workdir``, the call's own working directory."""
    try:
        if tool_name == "view_image":
            raw = _ViewImage.model_validate(tool_input).path
        elif tool_name == "Bash":
            return bash_read_path(_Bash.model_validate(tool_input).command, workdir)
        else:
            return None
    except ValidationError:
        return None
    return _absolute(raw, workdir) if raw else None


def bash_read_path(command: str, workdir: str | None) -> Path | None:
    raw = _bash_target(command)
    if not raw:
        return None
    # Allow only a lone, unquoted leading ``~``, which bash expands as we do.
    if "~" in command and (
        not raw.startswith("~") or command.count("~") != 1 or re.search(r"['\"\\]", command)
    ):
        return None
    return _absolute(raw, workdir)


def _bash_target(command: str) -> str | None:
    if _SHELL_META.search(command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    program, args = argv[0], argv[1:]
    if program == "sed":
        if len(args) != 3 or args[0] != "-n" or not _SED_RANGE.fullmatch(args[1]):
            return None
        if "$" in command and command.replace(f"'{args[1]}'", "", 1).count("$"):
            return None
        return args[2]
    if "$" in command:
        return None
    if program not in _VALUE_OPTIONS:
        return None
    paths: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg.startswith("-") and arg != "-":
            skip = arg in _VALUE_OPTIONS[program]
        else:
            paths.append(arg)
    return paths[0] if len(paths) == 1 else None


def _absolute(raw: str, workdir: str | None) -> Path | None:
    try:
        path = Path(raw).expanduser()
    except RuntimeError:  # ~unknownuser
        return None
    if not path.is_absolute():
        if not workdir:
            return None
        path = Path(workdir) / path
    return Path(os.path.normpath(path))
