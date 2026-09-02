"""Tests for the yaml_pytest decorator in slashid_ai_forwarder_core.testing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from slashid_ai_forwarder_core.testing import yaml_pytest

# --------------------------------------------------------------------------
# Sample model used by typed-coercion tests
# --------------------------------------------------------------------------


class _Sample(BaseModel):
    field: str
    count: int = 0


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# --------------------------------------------------------------------------
# Load-time validation
# --------------------------------------------------------------------------


def test_happy_path_untyped(tmp_path: Path) -> None:
    """A 2-case fixture with unannotated params parametrizes correctly."""
    _write(
        tmp_path,
        "cases.yaml",
        "id: a\nbody: {x: 1}\nexpected: 1\n---\nid: b\nbody: {x: 2}\nexpected: 2\n",
    )

    captured: list[tuple[Any, Any]] = []

    @yaml_pytest(filename="cases.yaml", base_dir=tmp_path)
    def _test(body, expected):
        captured.append((body, expected))

    # pytest.mark.parametrize decorator ran successfully — no exception.
    # We don't actually invoke via pytest here; that's what the migration
    # tests do end-to-end. Assert the mark is attached correctly.
    marks = getattr(_test, "pytestmark", [])
    assert len(marks) == 1
    mark = marks[0]
    assert mark.name == "parametrize"
    argnames, argvalues = mark.args
    assert argnames == "body,expected"
    assert list(argvalues) == [({"x": 1}, 1), ({"x": 2}, 2)]
    assert mark.kwargs["ids"] == ["a", "b"]


def test_default_filename_and_base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults resolve to <fn's source dir>/<fn.__name__>.yaml."""
    # Build a throwaway module in tmp_path with a function decorated by yaml_pytest().
    test_module = tmp_path / "test_dummy.py"
    fixture = tmp_path / "test_target.yaml"
    fixture.write_text("id: only\nbody: 42\n")
    test_module.write_text(
        "from slashid_ai_forwarder_core.testing import yaml_pytest\n"
        "@yaml_pytest()\n"
        "def test_target(body):\n"
        "    pass\n"
    )
    # exec the module and check the decoration succeeded
    import importlib.util

    spec = importlib.util.spec_from_file_location("_yaml_pytest_test_dummy", test_module)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    marks = getattr(mod.test_target, "pytestmark", [])
    assert len(marks) == 1
    argnames, argvalues = marks[0].args
    assert argnames == "body"
    assert list(argvalues) == [(42,)]
    assert marks[0].kwargs["ids"] == ["only"]


def test_explicit_filename_overrides_default(tmp_path: Path) -> None:
    _write(tmp_path, "custom.yaml", "id: c\nbody: 7\n")

    @yaml_pytest(filename="custom.yaml", base_dir=tmp_path)
    def _test(body):
        pass

    marks = getattr(_test, "pytestmark", [])
    assert marks[0].args[1] == [(7,)]


def test_duplicate_id_raises(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "dups.yaml",
        "id: same\nbody: 1\n---\nid: same\nbody: 2\n",
    )

    with pytest.raises(ValueError, match="duplicate case id 'same'"):

        @yaml_pytest(filename="dups.yaml", base_dir=tmp_path)
        def _test(body):
            pass


def test_non_string_id_raises(tmp_path: Path) -> None:
    _write(tmp_path, "badid.yaml", "id: 42\nbody: 1\n")

    with pytest.raises(ValueError, match="missing string `id` field"):

        @yaml_pytest(filename="badid.yaml", base_dir=tmp_path)
        def _test(body):
            pass


def test_missing_id_raises(tmp_path: Path) -> None:
    _write(tmp_path, "noid.yaml", "body: 1\n")

    with pytest.raises(ValueError, match="missing string `id` field"):

        @yaml_pytest(filename="noid.yaml", base_dir=tmp_path)
        def _test(body):
            pass


def test_empty_file_raises(tmp_path: Path) -> None:
    _write(tmp_path, "empty.yaml", "")

    with pytest.raises(ValueError, match="no cases in"):

        @yaml_pytest(filename="empty.yaml", base_dir=tmp_path)
        def _test(body):
            pass


def test_file_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="YAML fixture not found"):

        @yaml_pytest(filename="nonexistent.yaml", base_dir=tmp_path)
        def _test(body):
            pass


def test_missing_keys_raises(tmp_path: Path) -> None:
    _write(tmp_path, "short.yaml", "id: only\nbody: 1\n")

    with pytest.raises(ValueError, match=r"missing keys.*'expected'"):

        @yaml_pytest(filename="short.yaml", base_dir=tmp_path)
        def _test(body, expected):
            pass


def test_extra_keys_ignored(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "extras.yaml",
        "id: a\nnote: some provenance\nlink: http://example\nbody: 1\n",
    )

    @yaml_pytest(filename="extras.yaml", base_dir=tmp_path)
    def _test(body):
        pass

    marks = getattr(_test, "pytestmark", [])
    # Only `body` gets passed; `note` and `link` are silently dropped.
    assert marks[0].args[0] == "body"
    assert marks[0].args[1] == [(1,)]


def test_yaml_safe_load_rejects_python_tags(tmp_path: Path) -> None:
    # Static !!python/object tag should trigger yaml's ConstructorError,
    # not silently execute code.
    import yaml
    import yaml.constructor

    _write(
        tmp_path,
        "unsafe.yaml",
        "id: evil\nbody: !!python/object/apply:os.system ['echo pwned']\n",
    )
    with pytest.raises(yaml.constructor.ConstructorError):

        @yaml_pytest(filename="unsafe.yaml", base_dir=tmp_path)
        def _test(body):
            pass


def test_zero_param_test_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "any.yaml", "id: a\nbody: 1\n")

    with pytest.raises(ValueError, match="must declare at least one parameter"):

        @yaml_pytest(filename="any.yaml", base_dir=tmp_path)
        def _test():  # no params
            pass


# --------------------------------------------------------------------------
# Typed coercion
# --------------------------------------------------------------------------


def test_annotated_param_coerced_to_pydantic_model(tmp_path: Path) -> None:
    _write(tmp_path, "typed.yaml", "id: a\nbody: {field: hello, count: 3}\n")

    @yaml_pytest(filename="typed.yaml", base_dir=tmp_path)
    def _test(body: _Sample):
        pass

    marks = getattr(_test, "pytestmark", [])
    row = next(iter(marks[0].args[1]))
    assert isinstance(row[0], _Sample)
    assert row[0].field == "hello"
    assert row[0].count == 3


def test_annotated_param_generic_list(tmp_path: Path) -> None:
    _write(tmp_path, "list.yaml", "id: a\nbody: [1, 2, 3]\n")

    @yaml_pytest(filename="list.yaml", base_dir=tmp_path)
    def _test(body: list[int]):
        pass

    row = next(iter(getattr(_test, "pytestmark", [])[0].args[1]))
    assert row[0] == [1, 2, 3]


def test_mix_annotated_and_unannotated(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "mix.yaml",
        "id: a\nbody: {field: hi}\nexpected: {arbitrary: dict}\n",
    )

    @yaml_pytest(filename="mix.yaml", base_dir=tmp_path)
    def _test(body: _Sample, expected):  # `expected` unannotated
        pass

    row = next(iter(getattr(_test, "pytestmark", [])[0].args[1]))
    assert isinstance(row[0], _Sample)
    assert row[1] == {"arbitrary": "dict"}


def test_coercion_failure_includes_case_id_and_param(tmp_path: Path) -> None:
    # `field` is required (no default); missing it fails validation.
    _write(tmp_path, "bad.yaml", "id: broken\nbody: {count: 5}\n")

    with pytest.raises(ValueError) as exc_info:

        @yaml_pytest(filename="bad.yaml", base_dir=tmp_path)
        def _test(body: _Sample):
            pass

    msg = str(exc_info.value)
    assert "broken" in msg  # case id
    assert "body" in msg  # parameter name
    assert "field" in msg  # pydantic diagnostic mentions the missing field
