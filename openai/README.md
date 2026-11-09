# SlashID forwarder for ChatGPT Enterprise

Two things for an organization's OpenAI connection in SlashID:

- **ChatGPT Work hooks.** An MCP server with one tool, `slashid_preflight`, that
  the admin wires into Global `requirements.toml` as `mcp_tool` handlers for
  `UserPromptSubmit` and `PreToolUse`. Each call asks SlashID's AI preflight and
  answers with a block, a deny or `{}`. It runs where ChatGPT's cloud
  orchestrator runs hooks: Work Cloud with local computer access. Local-only
  Work threads use the Codex hook (`codex/`).
- **ChatGPT upload reports.** A tick reads `CONVERSATION_MESSAGE` files from the
  Compliance Logs Platform and reports every file a user attached to a message,
  so `SensitiveFileSharedWithAI` covers ChatGPT. Uploads cannot be stopped:
  ChatGPT has no hook for them.

The design, the decisions and what is still unconfirmed:
[`docs/superpowers/specs/2026-11-01-openai-chatgpt-work-design.md`](../docs/superpowers/specs/2026-11-01-openai-chatgpt-work-design.md).

## Routes

| Route | What |
| --- | --- |
| `POST /mcp` | MCP (streamable HTTP, JSON only). Requires a bearer token from the configured IdP. |
| `GET /.well-known/oauth-protected-resource[/mcp]` | RFC 9728 metadata naming the IdP, for ChatGPT's OAuth flow. |
| `POST /tick` | The upload reader. Cloud Scheduler's OIDC token, checked against `SLASHID_TICK_PRINCIPAL`. |
| `GET /healthz` | Liveness. |

## Configuration

Environment variables, all `SLASHID_`-prefixed:

| Variable | Meaning |
| --- | --- |
| `PUSH_TOKEN`, `ENDPOINT` | The OpenAI connection's event-streaming token, and the SlashID API. |
| `PUBLIC_URL` | This service's origin as ChatGPT reaches it. |
| `OIDC_ISSUER` | The IdP that issues the MCP connector's per-user tokens. Unset refuses every hook call. |
| `OIDC_AUDIENCE` | The audience those tokens carry. Default `<PUBLIC_URL>/mcp`. |
| `OIDC_JWKS_URL` | Optional; discovered from the issuer. |
| `OIDC_USER_CLAIM` | The claim holding the email. Default `email`. |
| `COMPLIANCE_KEY`, `WORKSPACE_ID` | Workspace-scoped key with the users read and `chatgpt.enterprise.compliance_logs_platform.conversation_message.read`. |
| `VERDICT_FAIL_MODE` | `allow` (default) or `deny` when there is no verdict. |
| `INCLUDE_RAW_CONTENT` | Send the prompt's text to preflight; content rules need it. |
| `PLATFORM` | `gcp` (Firestore, `PROJECT_ID`) or `local` (SQLite, a timer drives the tick). |
| `TICK_PRINCIPAL`, `TICK_AUDIENCE` | Who may call `/tick`. |

## Development

```bash
(cd openai && uv run pytest)
(cd openai && uv run ruff check . && uv run ruff format --check .)
(cd openai && uv run ty check)
docker build -f openai/Dockerfile .
```
