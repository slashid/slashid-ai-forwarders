# Codex collection: managed hooks, preflight and rollout events

**Date:** 2026-09-30
**Status:** Design, for review. Wire claims were measured on 2026-09-30 against Codex CLI `0.158.0-alpha.2.1` (bundled with the ChatGPT desktop app `26.924.51851`, Linux) and Bedrock `us-east-2`. Claims marked **unverified** are from documentation and need a capture before the plan relies on them.
**Target repo:** `slashid-ai-forwarder`, new workspace member `codex/`, plus `shared/` and `bedrock/`.
**Replaces:** `ng-evangelion/backend/modules/detections/components/aiauthorization/codex-client` (removal happens in ng-evangelion once this ships).
**Server side, already on main:** `POST /ip/nhi/events/ai-invocations/preflight` evaluates the AI hook policy ([ng-evangelion#7847](https://github.com/slashid/ng-evangelion/pull/7847)), `NormalizeAIInvocation` (#7846) and the OpenAI adapter's `ResolveAIInvocationIdentity`.

## Overview

A CLI, `slashid-codex`, that Codex runs as a managed hook on every endpoint. It does two jobs.

```
 Codex ──UserPromptSubmit──► slashid-codex hook ──preflight──► SlashID ──► allow / block
       ──PreToolUse───────►        "                  "
       ──Stop (async)─────► slashid-codex hook ──read rollout from offset──► push AIInvocationObservedV1
       ──SessionEnd───────►        "                  "
```

1. **Enforcement.** `UserPromptSubmit` and `PreToolUse` become preflight requests. The server's `deny_reasons` become Codex's block decision.
2. **Collection.** `Stop` and `SessionEnd` read the session's rollout JSONL (`transcript_path`) from a saved offset and push one `AIInvocationObservedV1` per model response. The rollout is Codex's own append-only session log under `~/.codex/sessions/`, the file `codex resume` replays; it holds every model item, the system prompt and per-response token usage, none of which hooks carry.

The OpenAI Responses format mapping lives in `shared/`, so the same normalizer also parses Bedrock MIL records for OpenAI models called through the Responses API.

## Goals

1. Feature parity with codex-client: deny prompts and tool calls by the organization's AI hook policy, fail closed by default.
2. Emit `AIInvocationObservedV1` for Codex activity, per model response, attributed to a configured OpenAI user.
3. One shared OpenAI Responses normalizer, used by Codex and by Bedrock.
4. Reuse `shared/` for events, hashing, batching, preflight and push.

## Non-goals

- **OpenAI Chat Completions.** A follow-up adds `shared/normalize/openai/completions/` and a Bedrock `InvokeModel` format (Gemma, gpt-oss and other open-weight models log Chat Completions bodies there).
- **Per-user credentials.** Identity comes from configuration in this version (see Security).
- **File checks on tool results.** `PostToolUse` is not hooked; `accessed_files` is not sent in preflight.
- **Codex Cloud and ChatGPT web/mobile.** Managed configuration does not apply to them.
- **Removing codex-client** from ng-evangelion.

## Background: measured wire shapes

### Hook payloads

Every event carries `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model` and `permission_mode` (`SessionEnd` omits the last two). `transcript_path` is present on every event.

| Event | Extra fields |
|---|---|
| `SessionStart` | `source` |
| `UserPromptSubmit` | `turn_id`, `prompt` |
| `PreToolUse` | `turn_id`, `tool_name` (`"Bash"`), `tool_input` (`{"command": …}`), `tool_use_id` (`"exec-<uuid>"`) |
| `PostToolUse` | as `PreToolUse`, plus `tool_response` (string) |
| `Stop` | `turn_id`, `stop_hook_active`, `last_assistant_message` |
| `SessionEnd` | `reason` |

Hooks carry no user identity, no token usage and no tool definitions.

### Rollout JSONL

`~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl`, append-only. Each line is `{timestamp, type, payload}`:

- `session_meta`: `id`, `session_id`, `originator` (`codex_exec`), `cli_version`, `model_provider`, `base_instructions`.
- `turn_context`: `turn_id`, `model`, sandbox and approval policy.
- `response_item`: Responses-API items. `message` (roles `developer`, `user`, `assistant`; assistant messages carry `phase`: `commentary` or `final_answer`), `custom_tool_call` / `custom_tool_call_output`, and (unverified) `function_call`, `reasoning`, `web_search_call`.
- `token_usage_record`: `response_id`, `turn_id`, `usage` (`input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`). One per model response, written after that response's output items.
- `event_msg`: `task_started`, `task_complete`, `token_count`, and `item_completed` whose `item` is a logical item: `UserMessage`, `AgentMessage`, `CommandExecution`, …

The model's tool is not the tool the hook reports. A shell command is a `custom_tool_call` named `exec` whose input is JavaScript (`tools.exec_command({cmd:"cat note.txt"})`), with `call_id` `call_…`. The hook reports `Bash` with `tool_use_id` `exec-<uuid>`. The `event_msg/item_completed` record of type `CommandExecution` carries the hook's id (`exec-<uuid>`). It shares no field with the model's call; the two are linked only by their order in the file (see Rollout → `RolloutInvocation`).

### Bedrock

- `bedrock-runtime.<region>.amazonaws.com/openai/v1/responses` accepts Responses requests (`us.openai.gpt-6-astra`). MIL logs them with `operation: "Responses"`: `inputBodyJson` is the request; `outputBodyJson` is the `Response` object, or for `stream: true` an array of SSE events whose `response.completed.response` holds the full object. Usage is `input_tokens`, `input_tokens_details.{cached_tokens, cache_write_tokens}`, `output_tokens`, `output_tokens_details.reasoning_tokens`.
- `bedrock-mantle.<region>.api.aws/v1/{responses,chat/completions}` works, but MIL records nothing for it.

## Components

### `shared/`

**`normalize/openai/`** (new)

- `responses/schema.py`: Pydantic models for the request (`input` as string or item list, `instructions`, `tools`), the `Response` (`output` items, `status`, `incomplete_details`, `usage`) and stream events (`response.completed` is the only one read). Item types: `message`, `reasoning`, `function_call`, `function_call_output`, `custom_tool_call`, `custom_tool_call_output`, `web_search_call`; unknown item types are kept as opaque and skipped by the normalizer.
- `responses/normalize.py`: `responses_to_normalized_invocation(request, response) -> NormalizedInvocation`. `instructions` and `developer`/`system` messages become the `system` message; `function_call`/`custom_tool_call` become `tool_use` blocks on the assistant message; their outputs become `tool_result` blocks on the following user message (matching the Anthropic convention `_used_tools` relies on); `reasoning` becomes `reasoning` with its summary text only (encrypted content is dropped). `tools` feed `build_tools_declared`.
- `stop_reasons.py`: `completed` with a tool call → `tool_use`; `completed` otherwise → `end_turn`; `incomplete` + `max_output_tokens` → `max_tokens`; `incomplete` + `content_filter` → `content_filtered`; `failed` → `error`; else `unknown`. Kept at `openai/` level for reuse by `completions/`.
- `usage.py`: Responses usage → `AIInvocationTokens`, additive like Vertex (`output` excludes thoughts): `cache_read = cached_tokens`, `cache_write = cache_write_tokens`, `input = input_tokens − cache_read − cache_write`, `reasoning = reasoning_tokens`, `output = output_tokens − reasoning`. OpenAI's `input_tokens` includes both cache counts and `output_tokens` includes reasoning (Bedrock capture: 23 output, 12 reasoning; Codex capture: 15189 − 0 − 15186 = 3 fresh input).

**`events.py`**

- `OpenAIIdentityDetails(kind="openai", service_account_id, user_id, api_key_id, api_key_hash)`, with the same at-least-one-identifier validator as `AnthropicIdentityDetails`. Mirrors the server's `OpenAIIdentityDetails`; `kind` is client-side. Added to the `IdentityDetails` union.

**`normalize/normalized/tools.py`**

- `resolve_tool(raw_name) -> (AITool, AIToolServer)`: `build_tools_declared([(raw_name, None, None)])` for one tool, so it reuses `parse_tool_name` unchanged (`mcp__s__t` → server `s`; `s__t` → runtime server `s`; bare names → synthetic `builtin`). Every tool lands on a named server, which is what the server's `joinAIToolUse` needs (see the parked "tool resolution in preflight policy" follow-up). Codex declares tools by name only everywhere, so a tool's id is the same in preflight and in pushed events.

### `bedrock/`

- `mil_normalize._FORMATS` gains `openai-responses` (request `ResponsesRequest`, response `Response`) and `openai-responses-stream` (response `list[ResponseStreamEvent]`, reduced to its `response.completed`). `on_parse` backfills cache tokens from `usage`, as the Anthropic entries do. The first match wins, so a test pins that a Responses record (`{input, model, store, stream}`) validates against none of the Anthropic or Converse adapters, and vice versa.
- `README.md` known limitations: `bedrock-mantle` traffic is not in MIL.

### `codex/` (new workspace member, package `slashid_codex`)

| Module | Responsibility |
|---|---|
| `config.py` | `CodexConfig(BaseConfig)` loaded from a file passed as `--config` (TOML): `endpoint`, `push_token_file`, `user_id`, `fail_mode` (`deny` default), `preflight_timeout_seconds` (4.0), `state_dir`, `include_raw_content`. Environment variables are not read, for config or transport: hooks inherit the user's environment. `endpoint` must be `https://` with no userinfo, query or fragment; the token must be at least 32 non-whitespace characters. |
| `http.py` | The one `httpx.AsyncClient` factory: `trust_env=False` (ignores `HTTPS_PROXY`, `SSL_CERT_FILE`, `.netrc`), `follow_redirects=False`, system CA store, explicit timeouts. Keeps codex-client's transport hardening. |
| `cli.py` | `slashid-codex hook --config <path> --event <Name>`. Reads stdin (bounded), dispatches, prints Codex's JSON output, always exits 0. |
| `hooks.py` | Pydantic models for the hook payloads above, one per event. |
| `preflight.py` | Builds the preflight invocation, calls `sink.preflight_invocation`, maps the verdict. |
| `rollout.py` | Pydantic models for rollout lines, and `rollout_invocations(lines) -> list[RolloutInvocation]`: one per `token_usage_record`, with the Responses request/response rebuilt from the items before it and tool calls renamed using the whole file (two passes, below). |
| `emit.py` | `Stop` / `SessionEnd` / `SessionStart` sweep: lock, read, build events, push, save the checkpoint. |
| `state.py` | Per-session checkpoint and lock under `state_dir`. |
| `deploy/requirements.toml` | The managed hook block (below). |

Install: MDM installs uv, then runs `uv tool install <wheel>` with `UV_TOOL_DIR=/opt/slashid/codex/tools` and `UV_TOOL_BIN_DIR=/opt/slashid/codex/bin` (Windows: `C:\ProgramData\SlashID\Codex\tools` and `…\bin`), as an administrator, so the executable lands at the path the managed block names and users cannot modify it. The wheel is published with each release. MDM also installs the config and the token file.

## Data flow

### Preflight: `UserPromptSubmit` and `PreToolUse`

Both build a partial `AIInvocationObservedV1`:

| Field | `UserPromptSubmit` | `PreToolUse` |
|---|---|---|
| `request_id` | `turn_id` | `f"{turn_id}:{tool_use_id}"` |
| `timestamp` | now | now |
| `identity_details` | `OpenAIIdentityDetails(user_id=config.user_id)` | same |
| `model` | `AIModel(id=model, provider="openai")` | same |
| `parsed_as` | `codex-hook` | `codex-hook` |
| `conversation_id` | `session_id` | `session_id` |
| `available_tool_servers`, `available_tools` | — | `resolve_tool(tool_name)` |
| `used_tools` | — | `[AIToolUse(tool_id, tool_use_id, is_error=False)]` |

The server normalizes this to `invoke_model` (both) plus `mcp_call{server, "tools/call", tool}` (`PreToolUse`), e.g. `mcp__payroll__read` → `mcp_call{payroll, read}`, `Bash` → `mcp_call{builtin, Bash}`.

Neither sends the prompt, tool arguments, `cwd` or the transcript, which keeps codex-client's privacy promise for the enforcement path.

Verdict:

- `deny_reasons == []` → print `{}` (allow).
- non-empty → `{"decision":"block","reason": <reasons joined by a space>}`. The same shape blocks both events; `continue:false` is never used (Codex treats it as a nonblocking failure on `PreToolUse`).
- `PreflightError`, config or token errors, invalid stdin, anything else → `fail_mode`. `deny` prints a block with a fixed reason; `allow` prints `{}`. The cause goes to stderr without payload content.

Consequences to document: a time-window rule on `invoke_model` also blocks tool calls in a turn already running when the window closes, which codex-client did not do. Hooking every tool (matcher `.*`) adds one preflight round-trip per tool call; deployments can narrow the matcher to `^mcp__`.

### Collection: `Stop` and `SessionEnd`

`emit.run(transcript_path, session_id)`:

1. Take the session lock (`state_dir/<session_id>.lock`, non-blocking; if held, exit: the holder will cover this turn or the next run will).
2. Load the checkpoint `{path, emitted_through_offset}`.
3. Parse the rollout from byte 0 up to the last complete line. Input for a response needs the whole history, so the file is always read from the start; only responses whose `token_usage_record` sits past `emitted_through_offset` are emitted. Re-parsing grows with the session; this is accepted (a 10 MB rollout parses well inside the async hook's budget), and a cached history snapshot is a later optimisation if measurements call for it.
4. For each such `RolloutInvocation`, build the event (below), then `push_invocations` in batches.
5. On success, save the checkpoint at the offset after the last emitted `token_usage_record`. On failure, save nothing: the next `Stop`, `SessionEnd` or sweep retries.

`SessionStart` (async) sweeps checkpoints whose rollout has grown past `emitted_through_offset` and whose session is not locked, so a push that failed on a session's last turn is retried the next time Codex starts. Checkpoints older than 7 days are deleted.

### Rollout → `RolloutInvocation`

Two passes over the parsed lines. The rename a response's output needs comes from lines written after that response closes (in the capture: `custom_tool_call`, `token_usage_record`, `item_completed{CommandExecution}`, `custom_tool_call_output`), so a single forward pass cannot do it.

1. **Index.** Map each `custom_tool_call.call_id` to its logical item(s) by the order rule below.
2. **Build.** Walk the lines again and build each response with the index applied, so a call has the same name in the response that made it and in every later history.

A call with no output yet (session killed, or the rollout read mid-write) keeps its raw form. Once that response is emitted and checkpointed it is never re-emitted, so its raw name is final.

In the build pass:

- `session_meta` sets `base_instructions`, `originator`, `cli_version`. `turn_context` sets the current `model` and `turn_id`.
- `response_item` lines append to a running item list. Items written since the previous `token_usage_record` that the model produced (assistant `message`, `reasoning`, `*_call`) are this response's output; everything before them is its input.
- Tool calls are renamed to their logical tool so names and ids equal what preflight saw. The model's `custom_tool_call` (`call_id` `call_…`) and the logical `event_msg/item_completed` item (`id` `exec-…`, the hook's `tool_use_id`) share no field; the only link is order. In the capture: call, `token_usage_record`, `item_completed{CommandExecution}`, call output. The rule: the `item_completed` tool items that appear between a `custom_tool_call` and its `custom_tool_call_output` belong to that call.
  - Exactly one `CommandExecution` item: the call becomes `function_call{name: "Bash", call_id: <item id>, arguments: {command: <item command>}}` and its output the matching `function_call_output`.
  - Anything else (zero items, several items from one `exec` script, overlapping parallel calls, item types not yet captured): the call keeps its raw form (`custom_tool_call` named `exec`, its own `call_id`). It is still a declared tool (`builtin/exec`) and still counts in `used_tools`, just not under the preflight's name.
  - Open question 2 captures these cases; each one moves from the fallback to a mapped form as it is verified.
- `custom_tool_call_output.output` is either a string or a list of `input_text` parts; the schema accepts both and the normalizer joins the parts' text.
- Top-level line types the parser does not model (`world_state`, `turn_context` fields it ignores, future types) are skipped silently. Only a line that is not valid JSON, or a modelled type that fails validation, counts as a parse failure.
- `token_usage_record` closes a response: request = `{instructions: base_instructions, input: <input items>}`, response = `{id: response_id, output: <output items>, status: "completed", usage}`. `status` is always `completed` for now, so an interrupted or aborted turn reports `end_turn` or `tool_use`; mapping `turn_aborted` waits for a capture (open question 3).
- A compaction record resets the running history to the compacted replacement (**unverified**: shape to be captured).

The pair goes through `responses_to_normalized_invocation`, then `build_event_from_normalized` with:

| Envelope field | Value |
|---|---|
| `request_id` | `response_id` |
| `timestamp` | the `token_usage_record` line's `timestamp` |
| `identity_details` | `OpenAIIdentityDetails(user_id=config.user_id)` |
| `model` | `AIModel(id=turn_context.model, provider="openai")` |
| `tokens` | `usage` via the Codex variant of `openai/usage.py` (`cached_input_tokens`, `cache_write_input_tokens`, `reasoning_output_tokens`). That `output_tokens` includes `reasoning_output_tokens`, as in OpenAI's API, is **unverified** for Codex (the capture had 0 reasoning); the capture in open question 2 checks it. |
| `parsed_as` | `codex-rollout` |
| `user_agent` | `f"{originator}/{cli_version}"` |
| `conversation_id` | `session_id` |

Attribution follows the existing rule: `input` is the full history, `used_tools` and `accessed_files` come from the round the response consumed.

The rollout records no tool definitions, so the request has no `tools`, and `_used_tools` (which resolves ids only through `input.tools_declared`) would drop every result. As the Anthropic hook does (`hook/envelope.py`, `build_tools_declared((name, None, None) …)`), the Codex path fills `tools_declared` and `tool_servers` itself from the names of every tool call in the history, after renaming. Name-only declaration makes these ids equal the preflight's `resolve_tool` ids. `available_tools` is therefore the set of tools used so far in the session.

With `include_raw_content` on, every event carries the full history in `input.redacted_text`, cut by the existing `max_content_size` (100 000 characters, middle-truncated) and batched under the 1 MB push limit by `push_invocations`. Hashes are always over the full, untruncated body.

### Managed configuration

```toml
[features]
hooks = true

[hooks]
managed_dir = "/opt/slashid/codex"
windows_managed_dir = 'C:\ProgramData\SlashID\Codex'
allow_managed_hooks_only = true

[[hooks.UserPromptSubmit]]
[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = "/opt/slashid/codex/bin/slashid-codex hook --config /opt/slashid/codex/config.toml --event UserPromptSubmit"
timeout = 10

[[hooks.PreToolUse]]
matcher = ".*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "/opt/slashid/codex/bin/slashid-codex hook --config /opt/slashid/codex/config.toml --event PreToolUse"
timeout = 10

[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "/opt/slashid/codex/bin/slashid-codex hook --config /opt/slashid/codex/config.toml --event Stop"
async = true
timeout = 120

[[hooks.SessionEnd]]
[[hooks.SessionEnd.hooks]]
type = "command"
command = "/opt/slashid/codex/bin/slashid-codex hook --config /opt/slashid/codex/config.toml --event SessionEnd"
timeout = 30

[[hooks.SessionStart]]
[[hooks.SessionStart.hooks]]
type = "command"
command = "/opt/slashid/codex/bin/slashid-codex hook --config /opt/slashid/codex/config.toml --event SessionStart"
async = true
timeout = 120
```

`allow_managed_hooks_only` sits under `[hooks]` as the documentation shows; codex-client puts it at the top level, which may be ignored (**unverified** either way; the plan tests it with a local `/etc/codex/requirements.toml`). Each entry also gets `command_windows = 'C:\ProgramData\SlashID\Codex\bin\slashid-codex.exe hook --config C:\ProgramData\SlashID\Codex\config.toml --event <Name>'`.

`async = true` is documented but **unverified** on this build. If Codex ignores it, `Stop` would hold the end of every turn for up to 120 s. The first plan task checks it; if unsupported, `Stop` and `SessionStart` get `timeout = 5` and the CLI detaches: it re-execs itself as a background child (`start_new_session` on POSIX, `DETACHED_PROCESS` on Windows) with the payload in a temp file, and returns `{}` at once.

## Security

- **Identity is claimed, not proven.** The push token is per OpenAI connection and readable by the user the hook runs as, so a user holding it can send any `user_id`, in preflight and in pushed events. This matches codex-client's stated posture ("managed client guardrails, not provider-signed attestations") but is weaker than its server-derived identity. Follow-up: per-user tokens that the server binds to a `user_id`.
- The config and token file are MDM-owned and not user-writable; the token never appears in hook arguments.
- Deny reasons are untrusted server text echoed to the user; they are passed through verbatim, never interpreted.
- Pushed content follows `include_raw_content` (off by default: hashes, mime and length only). This goes beyond codex-client, which sent no content; the README must say so.

## Error handling

| Failure | Behavior |
|---|---|
| Preflight unreachable, non-200, bad body, over budget | `fail_mode` (`deny` default) |
| Bad config or token file | Preflight: `fail_mode`. Collection: exit, nothing saved |
| Invalid hook stdin | Preflight: `fail_mode`. Collection: exit |
| Rollout line that fails to parse | Skipped and counted on stderr; a truncated last line is left for the next run |
| Push fails | Checkpoint not advanced; retried by the next `Stop`, `SessionEnd` or `SessionStart` sweep |
| Lock held | Exit without work |

The hook always exits 0 and prints valid JSON, so Codex never sees a crashed hook as a nonblocking failure.

## Testing

- **Fixtures** from the captures of 2026-09-30: the six hook payloads, the rollout, and the Bedrock MIL records (non-stream and stream). Rollout fixtures are trimmed of `base_instructions` and environment context.
- **shared:** Responses normalizer on both Bedrock records; stop reasons; usage; `resolve_tool`; `OpenAIIdentityDetails` validation.
- **bedrock:** `normalize_record` picks `openai-responses` and `openai-responses-stream`.
- **codex:** preflight invocation per event, checked against the server's join rule (every `used_tools` entry resolves to a named tool on a named server); verdict and fail-mode mapping; rollout → invocations (two responses, tool call renamed to `Bash` with the hook's id); checkpoint and retry; lock contention; CLI end to end through a subprocess with a stub server.
- **Live:** before merge, run Codex with the managed block against a dev SlashID endpoint and a pilot `user_id`; check allow, deny by model rule, deny by tool rule, fail-closed with the server down, and that the events land.

## Open questions

1. **Identity id space.** Does the configured `user-…` id (ChatGPT workspace user) match what the OpenAI connection syncs as `IdentifierFromSource`? If not, every preflight with a policy denies as `identity_absent`.
2. **Tool calls beyond Bash.** Only `Bash` has been captured. Capture MCP calls, `apply_patch`, web search, a failing command, parallel calls and one `exec` script running several commands. For each: whether `PreToolUse` fires, its `tool_name` (the `mcp__payroll__read` example and the `^mcp__` narrowing advice assume the documented `mcp__<server>__<tool>`), and its `item_completed` type, to replace the raw-call fallback.
3. **Compaction, resume, fork and interrupts.** Capture their rollout shapes, including `turn_aborted`. A forked session copies history into a new rollout; if that includes old `token_usage_record`s, the checkpoint must also store emitted `response_id`s and skip them to avoid re-emitting.
4. **Subagents.** Whether their tool hooks fire and which `session_id` / rollout they use.
