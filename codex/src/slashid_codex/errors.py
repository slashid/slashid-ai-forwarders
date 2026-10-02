"""Errors for ``daemon.log`` without payload content: a pydantic
``ValidationError`` quotes its input, in its text and in a traceback."""

from __future__ import annotations

import logging

from pydantic import ValidationError


def summary(exc: BaseException) -> str:
    """Each error's location and type for a ``ValidationError``, else the text."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors(include_input=False)
        )
    return str(exc)


def _validation_error(exc: BaseException | None) -> ValidationError | None:
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ValidationError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def log_failure(logger: logging.Logger, message: str, *args: object, exc: BaseException) -> None:
    """``message`` with a traceback, unless a validation error is in the
    chain: then its type and summary only."""
    if (invalid := _validation_error(exc)) is None:
        logger.error(message, *args, exc_info=exc, stacklevel=2)
    else:
        logger.error(
            message + ": %s: %s", *args, type(exc).__name__, summary(invalid), stacklevel=2
        )
