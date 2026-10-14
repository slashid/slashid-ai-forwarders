# Codex tool calls: every tool use, whatever the model's script looked like

**Date:** 2026-10-13
**Status:** Design. Observed on Codex desktop `26.924` (`codex-cli 0.158.0-alpha.2.1`, Linux), signed in with ChatGPT, through `codex exec --json`, the rollouts it wrote under `~/.codex/sessions/`, and a logging hook.
**Consumer:** `2026-10-13-connector-file-refs-design.md` (connector reads), and any feature that needs the tools a Codex response used.

## Problem

In script mode the model does not call tools directly. It calls the `exec` tool with JavaScript, and the script calls `tools.<name>(…)`:

```js
await Promise.allSettled([
  ["pdf", () => tools.mcp__codex_apps__google_drive_fetch({url: "…", download_raw_file: true, include_base64: true})],
  ["txt", () => tools.mcp__codex_apps__google_drive_fetch({url: "…"})],
].map(async ([k, f]) => { try { const r = await f(); store(k, r); text({name: k, result: r}); } catch (e) { text({name: k, error: String(e)}) } }));
```

A later script can compute arguments from an earlier result (`load("sharepoint").structuredContent.results.find(…).url`). The rollout's `response_item` carries only the `exec` `custom_tool_call` (the JS) and its output. `shell_calls.py` rebuilds a call when the script is exactly one `tools.NAME(ARG)` (bare, `text(await …)`, or the `image()` idiom); any other script is opaque, so the forwarder sees no tool use for it.

## What Codex records

1. **`response_item`s:** `custom_tool_call{name: "exec", input: <JS>}` and `custom_tool_call_output{output: [input_text, …]}`. The output is the host header `Script completed\nWall time <s> seconds\nOutput:\n` (written by the code-mode host in the `codex` binary, not by the tool or the script), then one block per `text()` emission. Long output is prefixed `Warning: truncated output (original token count: N)` and cut; a slow script yields `Script running with cell ID …` and its result arrives through a later `wait` call. A script that never calls `text()` shows nothing.
2. **`event_msg` `item_completed`** with `item.type: McpToolCall`, one per tool call the script made, whatever the script looked like:

   | Field | Example |
   | --- | --- |
   | `id` | `exec-<uuid>` |
   | `server`, `tool` | `codex_apps`, `google_drive.fetch` |
   | `arguments` | the computed arguments, e.g. `{url, download_raw_file, include_base64}` |
   | `connectorId`, `linkId`, `appName`, `actionName`, `readOnlyHint` | `connector_5f3c8c41…` (Drive), `connector_1e4f6a44…` (SharePoint) |
   | `status`, `duration` | |
   | `result` | `{content: [{type: "text", text: "Action completed."}], structuredContent: {…}, isError}` |

   `structuredContent` is the connector payload and is not truncated (a 43 KB `b64_string` decoded to the exact 32,478-byte PDF). Parallel calls complete in any order.
3. **`PreToolUse` / `PostToolUse` hooks**, once per tool called inside an `exec` script (two parallel `google_drive.fetch` calls gave two of each):

   | Call | `tool_name` | `tool_input` |
   | --- | --- | --- |
   | Drive fetch | `mcp__codex_apps__google_drive__fetch` | `{url}` |
   | SharePoint search | `mcp__codex_apps__microsoft_sharepoint__search` | `{query}` |
   | SharePoint fetch | `mcp__codex_apps__microsoft_sharepoint__fetch` | `{url: "https://graph.microsoft.com/v1.0/drives/<driveId>/items/<itemId>"}` |

   `tool_use_id` is the `McpToolCall` `id` (`exec-<uuid>`). The name differs from the script's (`tools.mcp__codex_apps__google_drive_fetch`): the hook separates connector and tool with `__`. `PostToolUse` carries the same fields plus `tool_response`, the item's `result` (`{content, structuredContent}`) whole; the forwarder registers only `PreToolUse`.

Today `rollout.py` parses `McpToolCall` into `OtherItem(type, id)`: `events.py` uses the ids for file records (`record_keys`), and `cursor.py` attaches a script's items to the last `exec` call (`Parallel exec calls: their items all join the last one`). Arguments and results are dropped. `failed_calls` looks only at `CommandExecution`.

## Design

Scope: the kinds of tool call an `exec` script makes that Codex records as items: MCP tools (any server), shell commands (`CommandExecution`), image views (`ImageView`) and patches (`FileChange`). Tools with no item and no hook (`update_plan`) are not tool uses of the event: the script is `exec`.

1. **Typed items.** `rollout.py` parses `McpToolCall` into a frozen model (`id`, `server`, `tool`, `arguments`, `status`, `result`) ahead of `OtherItem` in the `CodexItem` union. `result` is typed (`content`, `structured_content` ← `structuredContent`, `is_error` ← `isError`) and absent on a failed call. `status` was `completed` in every capture; any other value counts as failed (also for `FileChange`). `CommandExecution` gains `formatted_output`; `FileChange` (`id`, `changes: {path: {type, content, unified_diff, move_path}}`, `status`, `stdout`) is new. Unparseable items stay `OtherItem`.
2. **Calls come from the items.** For each closing response, every script-mode call item among its joined items (a function-mode item shares its real call's `call_id` and needs no pair) becomes a synthesized pair, `function_call` and `function_call_output` with `call_id` = the item's `id`:

   | Item | Call | Output |
   | --- | --- | --- |
   | `McpToolCall` | `mcp__<server>__<tool with "." → "__">`, `arguments` | the result serialized by alias (`structuredContent`, `isError`); without a result, `{"isError": <failed>}` |
   | `CommandExecution` | `Bash`, `{command, workdir}`: `command` is, for a known shell (`bash`, `sh`, `zsh`, `dash`, `ksh`, `fish`), the argument after the first short-option group containing `c` (`-c`, `-lc`, `-ilc`), else the joined argv; `workdir` is the path of the `file://` `cwd` | `formatted_output` |
   | `ImageView` | `view_image`, `{path}` from the `file://` URL | none |
   | `WebSearch` (`type: "Extension"`, `kind: "web.search"`) | `webrun`, `{query, action}`, as `web__run` opens a page or searches | `results` as JSON |
   | `FileChange` | `apply_patch`, `{changes}`: the item's `changes` as they are (path → `type`, `content` of an add, `unified_diff` and `move_path` of an update), without the `content` of a delete, which is the file's text and not something the model sent; the hook's argument is the patch text, which the item does not keep | `stdout` |

   `list_mcp_resources`, `list_mcp_resource_templates` and `read_mcp_resource` are Codex's own tools: the rollout records them as an `McpToolCall` of the server they ask about (`server: "paulo"`, `arguments: {server: "paulo"}`), but the hook names them bare, so the call is named by its tool alone. The names are the ones the hook reports (`mcp__codex_apps__google_drive__fetch`, `mcp__paulo__web_fetch`, `Bash`, `view_image`): `parse_tool_name` splits an MCP name at the first `__` after the server, so a connector tool is `google_drive__fetch`, and the preflight's `requested_tool_uses` and the event's `used_tools` get the same tool id. The `call_id` equals the hook's `tool_use_id`. The shared normalizers (`declared_from_calls`, tool server attribution, accessed-file extractors) treat the pair like any call.
3. **Errors.** `tool_is_error` is a normalized-block flag, set by `mark_errors(failed_calls(consumed_items))`. `failed_calls` returns the id of a `CommandExecution` that failed and of an `McpToolCall` whose `status` is not `completed` or whose result has `isError`; the id is now the synthesized call's, so script-mode command failures are flagged too.
4. **Placement.** In `cursor.py` `_close`, before `request.input` is built (today `_join` runs after it, and `after_last_assistant` takes what follows the last assistant message, a `function_call` being one). Within the round's items, the synthesized calls go immediately before the first tool output and the synthesized outputs immediately after the last one. The round stays calls-then-outputs, so every output remains after the last call, even when the round also holds an aborted response's calls or a later user prompt. The result goes into `request.input` and `committed`, so a later response sees the same calls. Pairs follow `_join`'s order: per call, in read order.
5. **No script rebuild.** `shell_calls.py` no longer parses a script into a call (`map_custom_call`, `parse_script`, the renamed output): a call appears once, and its result is the tool's, not what the script printed. Function mode's `exec_command` → `Bash` stays. The script's own `exec` call stays in the history, so the event's `requested_tool_uses` and `used_tools` list `exec` as well as the tools it ran, as they already do for an opaque script.
6. **Output parsing.** Synthesized pairs carry the tool's own output, no host header. A rebuilt call's output goes through `shell_stdout`, which accepts only the two-block shape (header, then the text); a `Warning: truncated output` or `Script running` output passes through as content, not as "no data".
7. **Round hashes.** The synthesized call is an assistant message that lands in the previous response's assistant run, so unflagged it changes that round's hash against the event that emitted it (measured: event 1's `round_hash` was missing from event 2's `recent_round_hashes`). The synthesized call carries `synthetic: true` (an optional field of `ResponsesFunctionCall`); `to_normalized` copies it to its `tool_use` block (`NormalizedContent.synthetic`, `None` otherwise, so other sources' serialized messages and hashes do not change), and `rounds.project` leaves synthetic blocks out. Only the call needs it: the output lands in the next round's consumed side, which every event projects alike. Rounds emitted before this change hashed a rebuilt single-call script as its tool (`Bash`), now as `exec`, so one stitch across the upgrade can miss.
8. **Preflight.** `_tool_invocation` builds `requested_tool_uses` from the hook's `tool_name` with `resolve_tool`, unchanged, and now agrees with the event. The items do not exist yet when `PreToolUse` fires, so the preflight sees the arguments only: a SharePoint fetch's `m365/item` ref is known before the call, a search's refs only after. `PostToolUse` has the result as the call finishes, before the rollout holds the item; registering it would give the result-only refs (`document_id`, search results) earlier, and nothing needs it today.

## Verified

- Discovery scripts (`ALL_TOOLS.filter(…)`) call no tool and produce no item.
- Hooks fire for MCP calls inside `exec`, with the payloads of "What Codex records" 3: Drive fetch, SharePoint search and fetch, `PostToolUse` `tool_response` for the SharePoint ones; a user MCP server (`paulo`, `web_fetch`) gives `McpToolCall{server: "paulo", tool: "web_fetch"}` and the hook name `mcp__paulo__web_fetch`.
- Live sessions through the real hooks (CLI, and the ChatGPT desktop app): every item had a `PreToolUse` preflight with the same tool id as the later event's `used_tools`, and `cat`, `view_image` files reached both. Two defects came out of it: the resource tools' names (above) and a script `view_image` reported twice (once hashed from the preflight record, once unhashed from the extractor: `drop_shadowed_stubs`).
- A script of two `tools.exec_command` calls gives two `CommandExecution` items (`exec-<uuid>`, argv `["/bin/bash", "-lc", "echo one"]`) and two `Bash` hooks with `{command}`; two `tools.view_image` calls give two `ImageView` items and two `view_image` hooks. The script's output mentions neither. A failed `view_image` writes no item (the hook fires, no `PostToolUse`); failed commands have `status: "failed"`.
- `tools.apply_patch` gives a `FileChange` item (add, update with `unified_diff` and `move_path`, delete with the file's `content`; `stdout` `Success. Updated the following files: …`) and an `apply_patch` hook with `{command: <patch text>}`. `tools.update_plan` gives neither an item nor a hook.

## Not covered

- Item types never captured here, whose names appear in the `codex` binary: `DynamicToolCall`, `CollabAgentToolCall` (sub-agents), `WebSearch`, `ImageGeneration`, `TerminalInteraction`, `HookPrompt`, review-mode items. The first two look like tool calls and are the next candidates.
- `apply_patch` is not read in the preflight: the patch is the model's own text and the tool returns `Success`, so no file content reaches the model.

- Items of a script still running when the response closes: after `_join` resets `_script_call` they are keyed by their own id and pruned, so they are lost, and the tool use with them (a rebuilt single-call script kept it before).
- The prompt path of the preflight (`_unconsumed`, `view()`) reads pending state, so MCP items of an interrupted response produce no pairs, and no refs, until the response closes.
- Function-mode (non-script) calls of connector tools: their hook and `function_call` names are unchecked.
- The `tool_response` of a fetch with `include_base64`, and the effect of `max_tokens` on `wait`.


## Fixtures

Record with `codex exec --json --skip-git-repo-check -s read-only "<prompt>"` (not `--ephemeral`, which writes no rollout), after signing in with ChatGPT and installing the Google Drive and SharePoint plugins; API-key sign-in offers a catalog without them. Ask for one tool call per `exec` script for a single-call case and several for a multi-call one. For hook payloads, run with a scratch `CODEX_HOME` (symlinked `auth.json` and `plugins`, its own `config.toml` and a logging `hooks.json`) and `--dangerously-bypass-hook-trust`.
