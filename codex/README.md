# slashid-codex

Codex managed hook for SlashID. `UserPromptSubmit` and `PreToolUse` become SlashID preflight verdicts (deny reasons block the prompt or tool call); the session rollouts (`~/.codex/sessions/…`) become one `AIInvocationObservedV1` per model response. Design: `docs/superpowers/specs/2026-09-30-codex-hooks-design.md`.

Each hook runs `slashid-codex hook`, a thin client (standard library and `platformdirs` only) that forwards the payload to a per-user daemon on `127.0.0.1`, starting it on first use. The daemon (`slashid-codex daemon`, FastAPI) answers preflight on a warm connection and pushes events from a worker thread. It exits after `daemon_idle_seconds` without hooks or published events; the next hook starts it again, and its startup sweep sends what was missed.

## Install

MDM, as administrator:

1. Install uv, then `uv tool install <wheel>` with `UV_TOOL_DIR=/opt/slashid/codex/tools` and `UV_TOOL_BIN_DIR=/opt/slashid/codex/bin` (Windows `C:\ProgramData\SlashID\Codex\tools` and `…\bin`), so users cannot modify it.
2. Install the config (`deploy/config.example.toml`) at `/opt/slashid/codex/config.toml` and the token file it names, both read-only to users.
3. Install `deploy/requirements.toml` as Codex's managed requirements.

Before an upgrade, stop the daemons (`<tool python> -m slashid_codex daemon` processes under the tool directory; Windows cannot replace a running `.exe`), never hook clients: a killed hook lets its action through. A daemon whose version differs from the hook's is replaced on the next hook; one whose config or token file changed notices within about 5 s and exits, and the next hook starts a new one.

Per-user state (`daemon.json`, `daemon.lock`, `daemon.log`, `daemon.stderr`, `spawn-failed`, `created_at`, `data.sqlite`) lives in `~/.local/share/slashid-ai-forwarder-codex`, `~/Library/Application Support/slashid-ai-forwarder-codex` or `%LOCALAPPDATA%\slashid\slashid-ai-forwarder-codex`. `daemon.log` is the daemon's rotated log; `daemon.stderr` holds only output that bypasses it, such as an interpreter crash. `--state-dir`, `--codex-home` and `--config-check-seconds` exist for tests.

## Config

| Field | Default | |
|---|---|---|
| `endpoint` | `https://api.slashid.com` | SlashID API origin, `https://` |
| `push_token_file` | | the OpenAI connection's token, ≥ 32 characters |
| `user_id` | | the ChatGPT workspace user (`user-…`) |
| `verdict_fail_mode` | `deny` | without a verdict (SlashID or the daemon unavailable, bad payload): `deny` blocks, `allow` allows |
| `preflight_timeout_seconds` | 4.0 | verdict budget from the hook's arrival, capped at 8 s |
| `max_file_bytes` | 50 MiB | larger files go without hashes |
| `include_raw_content`, `max_content_size` | off, 100000 | round text in events |
| `input_scope`, `round_link_depth` | `round`, 10 | |
| `codex_home` | `~/.codex` | does not read `CODEX_HOME` |
| `codex_bin` | | for `codex mcp list`; else `codex` on `PATH`, else the desktop bundle |
| `daemon_idle_seconds` | 600 | |
| `request_timeout_seconds` | 10.0 | per SlashID request |
| `max_retries` | 3 | per push request, on transient failures |
| `dry_run` | false | dev only, below |

Environment variables are never read.

## Development only

`dry_run = true`: preflight sleeps 1 s, logs the invocation to `daemon.log` and allows; pushes sleep 1 s, log the events (raw content included when `include_raw_content` is on) and succeed. No real SlashID token is needed, but `push_token_file` must still name a file of 32+ characters. Not for production.

## Notes

- Preflight sends file names and hashes (attachments and the file a tool call reads); codex-client sent none. No prompt text, tool arguments, file content or `cwd`.
- A time-window rule on `invoke_model` also blocks tool calls of a turn already running when the window closes.
- Collection runs on one worker, and each failed batch is retried, so an unreachable SlashID can hold it for minutes.
- The `PreToolUse` matcher `.*` costs one preflight round-trip per tool call; narrowing it gives up the read checks.

## Known limitations

- Script mode (`codex exec`): the hook's `tool_use_id` (`exec-<uuid>`) is not linked to the call's `call_id`.
- Reads are checked only for simple shell reads (`cat`, `sed`, `head`, `tail`, `nl` on one path) and `view_image`; other commands are reported after the fact.
- A token or config rotation takes effect within about 5 s (the daemon checks the files every 5 s and exits; the next hook respawns it).
- The spawn backoff after a failed start is keyed by the config file's size and mtime: fixing the config applies at once, but fixing only the token file does not reset it and can wait up to the 5-minute backoff window.
- Identity is claimed, not proven: the token is per connection and readable by the user, so a user can send any `user_id`.
- Daemon lifetime on macOS and Windows, and the macOS and Windows desktop bundle paths for `codex`, are unverified.
