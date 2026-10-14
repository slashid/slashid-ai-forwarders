"""Rollouts whose ``exec`` scripts call ``codex_apps`` MCP tools, derived from
the sanitised ``script`` fixture (its ``exec`` call is line 12, its
``CommandExecution`` item line 14)."""

from __future__ import annotations

import json
from pathlib import Path

ROLLOUTS = Path(__file__).parent / "fixtures" / "rollouts"
SCRIPT = (
    "const r = await tools.mcp__codex_apps__google_drive_fetch("
    '{url: "https://drive.google.com/file/d/abc/view"});\ntext(r);\n'
)
URL = "https://drive.google.com/file/d/abc/view"


def mcp_item(item_id: str, *, status: str = "completed", is_error: bool = False) -> dict:
    return {
        "type": "McpToolCall",
        "id": item_id,
        "server": "codex_apps",
        "tool": "google_drive.fetch",
        "arguments": {"url": URL},
        "status": status,
        "result": {"content": [], "structuredContent": {"id": "abc"}, "isError": is_error},
    }


def write_mcp_rollout(tmp_path: Path, items: list[dict]) -> Path:
    """``script`` with the ``exec`` running MCP calls, one ``item_completed`` per item."""
    lines = [json.loads(raw) for raw in (ROLLOUTS / "script.jsonl").read_bytes().splitlines()]
    assert lines[12]["payload"]["name"] == "exec"
    assert lines[14]["payload"]["item"]["type"] == "CommandExecution"
    lines[12]["payload"]["input"] = SCRIPT
    template = lines[14]
    replacement = []
    for item in items:
        line = json.loads(json.dumps(template))
        line["payload"]["item"] = item
        replacement.append(line)
    lines[14:15] = replacement
    path = tmp_path / "mcp.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path
