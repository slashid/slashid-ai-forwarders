# OpenAI forwarder: ChatGPT Work hooks and ChatGPT upload reports

ng-evangelion #8943 declares, for an OpenAI connection, that a preflight policy
blocks prompts, files and tool calls in ChatGPT Work as well as in Codex, and
that files uploaded to ChatGPT are reported afterwards through the ChatGPT
Enterprise Compliance API. This component makes both true. Codex itself keeps
its MDM-installed hook (`codex/`); nothing here changes it.

## What OpenAI gives us

**Local-only Work threads** keep the managed hooks a Codex install already has,
so the `codex/` hook covers them with no change.

**Work Cloud with local computer access** runs its orchestrator on OpenAI's
side, and only admin-managed remote MCP hooks run there: `mcp_tool` handlers in
the Global `requirements.toml` (or Agent Security), each naming a tool on an
*already-connected* MCP server. Work Cloud without local access and personal
accounts get no hooks at all: those stay unenforceable, which #8943's notes say.

The `mcp_tool` contract, read from openai/codex (`67f0994b53`, execution merged
in #38705 and #39296):

- The tool's arguments come only from the handler's `input` template;
  `${field.path}` placeholders are filled from the hook event, a whole-value
  placeholder keeping its JSON type. A placeholder naming a missing field
  fails the hook.
- Event fields: `session_id`, `turn_id`, `transcript_path`, `cwd`,
  `hook_event_name`, `model`, `permission_mode`, plus `prompt` on
  `UserPromptSubmit` and `tool_name`, `tool_input`, `tool_use_id` on
  `PreToolUse`. **No field names the user.**
- The result is read from the text content blocks, as one JSON object with
  unknown fields rejected. `{}` allows. A prompt is blocked by
  `{"decision":"block","reason":"…"}`; a tool call is denied by
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"…"}}`.
- `isError`, a timeout, a malformed result or an unreachable server *fail the
  hook without blocking* (fail-open, openai/codex#41979 proposes otherwise).

## Decisions

**1. A stateless MCP server on `POST /mcp`** (streamable HTTP, JSON responses
only, no sessions), exposing one tool, `slashid_preflight`. It answers
`initialize`, `ping`, `tools/list` and `tools/call`, and 202 to notifications.
The handler maps the event onto the same `AIInvocationObservedV1` the Codex hook
sends and asks `POST /ip/nhi/events/ai-invocations/preflight` with the
connection's push token, so the policy, its evaluator and its detections are
the ones Codex already uses. A prompt becomes an `invoke_model` (its text in
`input.redacted_text` when `SLASHID_INCLUDE_RAW_CONTENT` is on, the same switch
the Anthropic hook uses; a content rule decides nothing without it). A tool
call becomes one `requested_tool_uses` entry resolved like Codex's, plus the
files `get_files_read_by_tool` finds in its input, by name: the files live on
the user's computer, so no hash can be taken here.

**2. The user comes from the MCP connection's OAuth token.** The hook reuses the
user's own connection to the server, and a ChatGPT custom MCP connector
authenticates per user. The server is an OAuth *resource server* only: it
publishes `/.well-known/oauth-protected-resource` naming the customer's IdP
(Okta, Entra, ...) as the authorization server, and validates each bearer token
as a JWT against that issuer's JWKS, its audience being this service. The email
claim (`SLASHID_OIDC_USER_CLAIM`, default `email`) is the user. No token, or
one that does not verify, is a 401 with `WWW-Authenticate`, which is what makes
ChatGPT run the OAuth flow. We issue no tokens and hold no client secrets.

*My call, unconfirmed against a live workspace:* that the cloud orchestrator
presents the per-user token on hook calls, as it does on ordinary tool calls
over the same connection. If it presents a workspace-level credential instead,
no hook call can name a user and this design does not enforce; see "Open".

**3. Email → ChatGPT user id in the forwarder.** ng-evangelion resolves an
OpenAI principal by its `user-…` id, which the token does not carry. The
forwarder lists the workspace's users (`GET /compliance/workspaces/{id}/users`)
with the compliance key, keeps the map in memory, refreshes it every
`SLASHID_DIRECTORY_TTL_SECONDS` and at most once a minute on a miss. An email
that is still unknown gets the `SLASHID_VERDICT_FAIL_MODE` answer (default
`allow`: refusing every prompt of a user the directory has not seen yet is
self-inflicted downtime, and the server would deny an unresolved identity
anyway). No server change is needed.

**4. ChatGPT uploads through the Compliance Logs Platform, on `POST /tick`.**
The conversations listing was removed in June 2026, so the reader pages
`GET /compliance/workspaces/{id}/logs?event_type=CONVERSATION_MESSAGE&after=…`,
downloads each JSONL file (`GET …/logs/{id}`, a 307 to a signed URL), and emits
one `AIInvocationObservedV1` per user message that carries `files`: the actor's
`user_id`, one `AIAccessedFile` per file by name with `provenance=attachment`,
`request_id` `chatgpt:<conversation>:<message>`. The log names a file but gives
no size or hash, so `SensitiveFileSharedWithAI` can match it by name only, its
weakest grade. Lines are delivered at least once, so a tick dedupes on
`event_id`. The watermark is `last_end_time` plus the last file id, saved after
each file is pushed, so a crash re-reads at most one file. A cold start begins
at now: a backfill would re-report the whole 30-day retention.

**5. Hosting like the Anthropic forwarder:** FastAPI on Cloud Run, Firestore for
the watermark and the tick lease, Cloud Scheduler driving `/tick` with an OIDC
token checked against `SLASHID_TICK_PRINCIPAL`. The `local` platform runs the
tick on a timer for development. Terraform and the release workflow are a
follow-up; this change ships the service, its image and its tests.

## Configuration

| Variable | Meaning |
| --- | --- |
| `SLASHID_PUSH_TOKEN`, `SLASHID_ENDPOINT` | the OpenAI connection's event-streaming token |
| `SLASHID_PUBLIC_URL` | this service's origin; the MCP resource is `<url>/mcp` |
| `SLASHID_OIDC_ISSUER`, `SLASHID_OIDC_AUDIENCE` | the IdP that issues the connector's tokens, and the audience they carry (default: the resource URL) |
| `SLASHID_OIDC_JWKS_URL` | optional; discovered from the issuer otherwise |
| `SLASHID_COMPLIANCE_KEY`, `SLASHID_WORKSPACE_ID` | Compliance API key and ChatGPT workspace; both needed for the directory and the upload reader |

The key needs the workspace users read and
`chatgpt.enterprise.compliance_logs_platform.conversation_message.read`.

## The admin's `requirements.toml`

Two handlers, one per event, because a placeholder naming a field the event
lacks fails the hook:

```toml
[[hooks.UserPromptSubmit]]

[[hooks.UserPromptSubmit.hooks]]
type = "mcp_tool"
server = "slashid"
tool = "slashid_preflight"
timeout = 10
statusMessage = "Checking with SlashID"
input = { hook_event_name = "${hook_event_name}", session_id = "${session_id}", turn_id = "${turn_id}", model = "${model}", prompt = "${prompt}" }

[[hooks.PreToolUse]]

[[hooks.PreToolUse.hooks]]
type = "mcp_tool"
server = "slashid"
tool = "slashid_preflight"
timeout = 10
statusMessage = "Checking with SlashID"
input = { hook_event_name = "${hook_event_name}", session_id = "${session_id}", turn_id = "${turn_id}", model = "${model}", cwd = "${cwd}", tool_name = "${tool_name}", tool_input = "${tool_input}", tool_use_id = "${tool_use_id}" }
```

`model` is optional on the event; if a deployment sees hooks fail on it, drop
it from the templates and the tool reports the model as `unknown`.

## Open

- Run it on a real Enterprise workspace. Check three things: that hooks are
  called (openai/codex#37363 parsed `mcp_tool` and skipped executing it before
  #38705), which token they carry, and the fail-open behaviour.
- Upload hashes: the old per-user file download
  (`/users/{user_id}/files/{file_id}`, 48-hour hold) is unconfirmed today. If
  it still works, hashing uploads lifts the match from name-only to content
  hash.
- ng-evangelion's own OpenAI compliance client calls `api.openai.com` without a
  workspace id, which the Compliance API does not serve. Fix that separately.
