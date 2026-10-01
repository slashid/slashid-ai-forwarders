# slashid-codex

Codex managed hook and per-user daemon for SlashID. Documentation lands with the deployment chunk.

`dry_run` is dev-only: it logs full event JSON to `daemon.log`, raw content included when `include_raw_content` is on. `codex_home` does not read `CODEX_HOME`.

Configured MCP servers come from `codex mcp list --json`: `codex_bin`, else `codex` on `PATH`, else the desktop bundle (`/usr/lib/chatgpt/resources/codex` on Linux; the macOS and Windows bundle paths are unverified).
