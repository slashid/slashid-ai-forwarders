"""One hook event in the daemon: validation, then preflight or a collection
trigger."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

from pydantic import ValidationError

from .cache import SessionCache
from .config import CodexConfig
from .emit import Trigger
from .hooks import (
    PreToolUseHook,
    SessionEndHook,
    SessionStartHook,
    StopHook,
    UserPromptSubmitHook,
    parse_hook,
)
from .preflight import Verdict, fail_verdict

log = logging.getLogger(__name__)

PREFLIGHT_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse"})
TRIGGER_EVENTS = frozenset({"Stop", "SessionStart", "SessionEnd"})
# Inside the client's 9 s preflight deadline, with room for the reply.
MAX_VERDICT_S = 8.0
INVALID_PAYLOAD = "Invalid Codex hook payload."
DAEMON_ERROR = "The SlashID Codex daemon failed."


class PreflightService(Protocol):
    async def user_prompt_submit(
        self, hook: UserPromptSubmitHook, *, deadline: float | None = None
    ) -> Verdict: ...

    async def pre_tool_use(
        self, hook: PreToolUseHook, *, deadline: float | None = None
    ) -> Verdict: ...


class Handler:
    def __init__(
        self,
        config: CodexConfig,
        preflight: PreflightService,
        cache: SessionCache,
        submit: Callable[[Trigger], None],
    ) -> None:
        self._config = config
        self._preflight = preflight
        self._cache = cache
        self._submit = submit

    def deadline(self, arrival: float) -> float:
        return arrival + min(self._config.preflight_timeout_seconds, MAX_VERDICT_S)

    async def preflight(self, event: str, payload: bytes, arrival: float | None = None) -> Verdict:
        """``arrival`` is the hook's ``time.monotonic()`` arrival; the verdict
        budget counts from it."""
        deadline = self.deadline(time.monotonic() if arrival is None else arrival)
        try:
            hook = parse_hook(event, payload)
        except ValueError as exc:
            log.warning("invalid %s payload: %s", event, _summary(exc))
            return fail_verdict(self._config, INVALID_PAYLOAD)
        self._cache.touch_hook(hook.session_id)
        try:
            if isinstance(hook, UserPromptSubmitHook):
                return await self._preflight.user_prompt_submit(hook, deadline=deadline)
            if isinstance(hook, PreToolUseHook):
                return await self._preflight.pre_tool_use(hook, deadline=deadline)
        except Exception:
            log.exception("preflight for %s failed", hook.session_id)
            return fail_verdict(self._config, DAEMON_ERROR)
        return fail_verdict(self._config, INVALID_PAYLOAD)

    def trigger(self, event: str, payload: bytes) -> None:
        """An invalid payload is ignored."""
        try:
            hook = parse_hook(event, payload)
        except ValueError as exc:
            log.warning("invalid %s payload: %s", event, _summary(exc))
            return
        self._cache.touch_hook(hook.session_id)
        if isinstance(hook, SessionStartHook | StopHook | SessionEndHook):
            self._submit(Trigger.from_hook(hook))


def _summary(exc: ValueError) -> str:
    """The error without payload content."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors(include_input=False)
        )
    return str(exc)
