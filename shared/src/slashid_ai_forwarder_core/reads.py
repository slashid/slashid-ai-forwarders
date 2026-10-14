"""The files a tool call is about to read, when the call is simple enough to tell.

Shared by Codex's ``PreToolUse`` preflight (every file) and by ``_READ_TOOLS``
(a call that reads exactly one, whose output is that file). ``None`` or an
empty list means "not a recognised read", never an error.
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
_SHELL_META = re.compile(r"[|;&<>`*?\[{}()^\n\r]")
_SED_RANGE = re.compile(r"[0-9$]+(,[0-9$]+)?p")
# Options that take a value, per reader.
_VALUE_OPTIONS = {
    "cat": set(),
    "nl": {"-b", "-d", "-f", "-h", "-i", "-l", "-n", "-s", "-v", "-w"},
    "head": {"-n", "-c"},
    "tail": {"-n", "-c", "-s"},
}


class _Bash(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str


class _ViewImage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


def get_file_read_by_tool(
    tool_name: str, tool_input: JsonValue, workdir: str | None, *, expand_home: bool = False
) -> Path | None:
    """``view_image`` → its ``path``; ``Bash`` → the one path of a plain
    ``cat``/``head``/``tail``/``nl`` or ``sed -n '<range>p'``. Relative paths
    resolve against ``workdir``, the call's own working directory. A ``~``
    path is kept as is unless ``expand_home``, which only the user's own
    machine may set."""
    try:
        if tool_name == "view_image":
            raw = _ViewImage.model_validate(tool_input).path
        elif tool_name == "Bash":
            command = _Bash.model_validate(tool_input).command
            return bash_read_path(command, workdir, expand_home=expand_home)
        else:
            return None
    except ValidationError:
        return None
    return _absolute(raw, workdir, expand_home) if raw else None


def get_files_read_by_tool(
    tool_name: str, tool_input: JsonValue, workdir: str | None, *, expand_home: bool = False
) -> list[Path]:
    """Like ``get_file_read_by_tool``, for every file: ``cat a b c`` reads three."""
    if tool_name != "Bash":
        path = get_file_read_by_tool(tool_name, tool_input, workdir, expand_home=expand_home)
        return [path] if path else []
    try:
        command = _Bash.model_validate(tool_input).command
    except ValidationError:
        return []
    return bash_read_paths(command, workdir, expand_home=expand_home)


def bash_read_path(command: str, workdir: str | None, *, expand_home: bool = False) -> Path | None:
    """The file of a command that reads exactly one."""
    raws = _bash_targets(command)
    if len(raws) != 1 or raws[0] == "-" or not _tilde_safe(command, raws):
        return None
    return _absolute(raws[0], workdir, expand_home)


def bash_read_paths(command: str, workdir: str | None, *, expand_home: bool = False) -> list[Path]:
    """Every file of a command that reads files, once each, in order. Standard
    input and a word that does not resolve are left out."""
    raws = [raw for raw in _bash_targets(command) if raw != "-"]
    if not _tilde_safe(command, raws):
        return []
    paths = (_absolute(raw, workdir, expand_home) for raw in raws)
    return list(dict.fromkeys(path for path in paths if path is not None))


def _tilde_safe(command: str, raws: list[str]) -> bool:
    """Allow only unquoted ``~`` that open a path word, which bash expands as we do."""
    if "~" not in command:
        return True
    return command.count("~") == sum(raw.startswith("~") for raw in raws) and not re.search(
        r"['\"\\]", command
    )


def _bash_targets(command: str) -> list[str]:
    if _SHELL_META.search(command):
        return []
    try:
        argv = shlex.split(command)
    except ValueError:
        return []
    # zsh expands ``=cmd`` to the command's path.
    if not argv or any(arg.startswith("=") for arg in argv):
        return []
    program, args = argv[0], argv[1:]
    if program == "sed":
        if len(args) < 3 or args[0] != "-n" or not _SED_RANGE.fullmatch(args[1]):
            return []
        if "$" in command and command.replace(f"'{args[1]}'", "", 1).count("$"):
            return []
        return args[2:]
    if "$" in command:
        return []
    if program not in _VALUE_OPTIONS:
        return []
    paths: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg.startswith("-") and arg != "-":
            skip = arg in _VALUE_OPTIONS[program]
        else:
            paths.append(arg)
    return paths


def _absolute(raw: str, workdir: str | None, expand_home: bool) -> Path | None:
    if raw.startswith("~"):
        # Only ``~`` and ``~/…``; ``~user`` is never resolved.
        if raw != "~" and not raw.startswith("~/"):
            return None
        if not expand_home:
            return Path(raw)
    try:
        path = Path(raw).expanduser()
    except RuntimeError:  # no home directory
        return None
    if not path.is_absolute():
        if not workdir:
            return None
        path = Path(workdir) / path
    return Path(os.path.normpath(path))
