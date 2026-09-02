"""Helpers for constructing wire-shape ``AITool`` from Python-native inputs.

The wire model's ``input_schema`` / ``output_schema`` are ``str | None``
(JSON-serialized), matching the SlashID OpenAPI spec. Normalizers hold
schemas as ``dict`` (that's what JSON Schema is), so this helper does the
one-way serialization at the boundary. Uses compact separators +
sort_keys so the serialization is stable across runs — hash-friendly.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic.json_schema import JsonSchemaValue

from ...events import AITool


def make_ai_tool(
    *,
    id: str,
    name: str | None = None,
    description: str | None = None,
    input_schema: JsonSchemaValue | None = None,
    output_schema: JsonSchemaValue | None = None,
    **kwargs: Any,
) -> AITool:
    """Build an ``AITool`` from JSON-Schema dicts.

    Serializes schemas to compact, key-sorted JSON strings for stable
    hashing. ``kwargs`` are forwarded verbatim so callers can pass
    ``tool_server_id`` / ``annotations`` / etc. without a wrapper.
    """
    return AITool(
        id=id,
        name=name,
        description=description,
        input_schema=_dump(input_schema),
        output_schema=_dump(output_schema),
        **kwargs,
    )


def _dump(schema: JsonSchemaValue | None) -> str | None:
    if schema is None:
        return None
    return json.dumps(schema, sort_keys=True, separators=(",", ":"))
