"""YAML-fixture-driven pytest parametrization with pydantic-typed coercion.

Usage (from any test module in any subproject):

    from slashid_ai_forwarder_core.testing import yaml_pytest

    @yaml_pytest()
    def test_anthropic_message_to_converse(
        body: AnthropicMessage,
        expected: ConverseResponse,
    ) -> None:
        assert message_to_converse(body) == expected

Defaults (both keyword-only, overridable):

- ``filename`` defaults to ``f"{fn.__name__}.yaml"``.
- ``base_dir`` defaults to the directory containing the decorated
  function's source file (so fixtures colocate with tests).

Combined default lookup: ``<test-file's dir>/<test-function-name>.yaml``.

YAML doc shape: each doc has an ``id`` key (used as the pytest
parametrize ID for readable failure output) plus keys matching the
decorated function's parameter names. ``id`` is not passed to the
test function — it's metadata for pytest only. Extra keys in the doc
(``note:``, ``captured_at:``, ``link:``, etc.) are ignored.

Parameters with type annotations get their YAML value coerced through
``pydantic.TypeAdapter(annotation).validate_python(...)`` before the
test runs. Unannotated parameters get the raw YAML value.

Validation errors (duplicate ids, missing keys, empty file, failed
coercion) raise at import time — one clear failure instead of N
cryptic test failures.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError


def yaml_pytest(
    *,
    filename: str | None = None,
    base_dir: Path | None = None,
) -> Callable[[Callable], Callable]:
    """Parametrize a test from a multi-doc YAML fixture file with typed coercion."""

    def decorator(fn: Callable) -> Callable:
        # Resolve defaults from the decorated function.
        # ty flags `Callable.__name__` / `Callable.__code__` because the abstract
        # Callable protocol doesn't guarantee them; real function objects always
        # have both. See ty typing-faq: "why does ty say Callable has no attribute".
        fn_name: str = fn.__name__  # ty: ignore[unresolved-attribute]
        fn_code_filename: str = fn.__code__.co_filename  # ty: ignore[unresolved-attribute]
        resolved_filename = filename or f"{fn_name}.yaml"
        resolved_base_dir = base_dir or Path(fn_code_filename).parent
        path = resolved_base_dir / resolved_filename

        if not path.exists():
            raise FileNotFoundError(f"YAML fixture not found: {path}")
        docs: list[dict[str, Any]] = [d for d in yaml.safe_load_all(path.read_text()) if d]
        if not docs:
            raise ValueError(f"no cases in {path}")

        sig = inspect.signature(fn)
        # get_type_hints resolves string annotations (needed under
        # `from __future__ import annotations`) and PEP 604 union syntax.
        hints = typing.get_type_hints(fn)
        params = list(sig.parameters)
        if not params:
            raise ValueError(
                f"@yaml_pytest test {fn_name!r} must declare at least one "
                "parameter — otherwise there is nothing to parametrize."
            )
        # Build one TypeAdapter per annotated parameter. Reused across every
        # case, so pydantic's per-adapter construction cost is paid once.
        # Parameters without a type annotation fall through as raw YAML.
        adapters: dict[str, TypeAdapter] = {  # type: ignore[type-arg]
            p: TypeAdapter(hints[p]) for p in params if p in hints
        }
        argnames = ",".join(params)
        argvalues: list[tuple] = []
        ids: list[str] = []
        seen: set[str] = set()
        for doc in docs:
            case_id = doc.get("id")
            if not isinstance(case_id, str):
                raise ValueError(f"case in {path} missing string `id` field")
            if case_id in seen:
                raise ValueError(f"duplicate case id {case_id!r} in {path}")
            seen.add(case_id)
            missing = set(params) - set(doc)
            if missing:
                raise ValueError(f"case {case_id!r} in {path} missing keys: {sorted(missing)}")
            row: list[Any] = []
            for pname in params:
                raw = doc[pname]
                if pname in adapters:
                    try:
                        row.append(adapters[pname].validate_python(raw))
                    except ValidationError as e:
                        raise ValueError(
                            f"case {case_id!r} in {path}: parameter {pname!r} "
                            f"failed pydantic validation:\n{e}"
                        ) from e
                else:
                    row.append(raw)
            argvalues.append(tuple(row))
            ids.append(case_id)
        return pytest.mark.parametrize(argnames, argvalues, ids=ids)(fn)

    return decorator
