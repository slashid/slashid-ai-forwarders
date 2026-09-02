"""Helpers for constructing wire-shape ``AITool`` / ``AIToolServer`` lists
from Python-native inputs.

The wire model's ``input_schema`` / ``output_schema`` are ``str | None``
(JSON-serialized), matching the SlashID OpenAPI spec. Normalizers hold
schemas as ``dict`` (that's what JSON Schema is), so ``make_ai_tool``
does the one-way serialization at the boundary. Uses compact separators
+ sort_keys so the serialization is stable across runs — hash-friendly.

``build_tools_declared`` is the vendor-agnostic factory: each vendor's
``_to_input`` passes an iterable of ``(raw_name, description, input_schema)``
tuples; the helper parses names via ``parse_tool_name`` (`mcp__server__tool`
/ `server__tool` / bare) and produces canonical ``AITool`` / ``AIToolServer``
lists with stable, content-derived ids.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from pydantic.json_schema import JsonSchemaValue

from ...events import AITool, AIToolServer, parse_tool_name


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


def _short_hash(obj: str | dict[str, object]) -> str:
    if isinstance(obj, str):
        data = obj.encode()
    else:
        data = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def build_tools_declared(
    raw_specs: Iterable[tuple[str, str | None, JsonSchemaValue | None]],
) -> tuple[list[AITool], list[AIToolServer]]:
    """Turn an iterable of ``(raw_name, description, input_schema)`` triples
    into deduplicated ``(tools, tool_servers)`` lists.

    Server id = ``short_hash({name, kind})``; tool id = ``short_hash({server,
    name, description, input_schema})`` — same content-derived recipe the
    envelope side used pre-canonical, so hashes are stable across the
    migration.

    Empty / whitespace-only names are skipped (matches the previous
    ``_available_tools`` behaviour on toolConfig entries with missing
    ``toolSpec.name``).
    """
    servers_by_id: dict[str, AIToolServer] = {}
    tools: list[AITool] = []
    for raw_name, description, input_schema in raw_specs:
        if not raw_name:
            continue
        tool_name, server_name, server_kind = parse_tool_name(raw_name)
        server_id = _short_hash({"name": server_name, "kind": server_kind})
        tool_id = _short_hash(
            {
                "server": server_name,
                "name": tool_name,
                "description": description or None,
                "input_schema": input_schema or None,
            }
        )
        if server_id not in servers_by_id:
            servers_by_id[server_id] = AIToolServer(
                id=server_id,
                name=server_name,
                kind=server_kind,
            )
        tools.append(
            make_ai_tool(
                id=tool_id,
                name=tool_name,
                description=description or None,
                input_schema=input_schema or None,
                tool_server_id=server_id,
            )
        )
    return tools, list(servers_by_id.values())
