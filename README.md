# slashid-ai-forwarders

Monorepo of SlashID forwarders that observe AI-provider invocations and forward metadata (identity, model, tokens, tool use) to SlashID's NHI subgraph. Some also ask SlashID for a verdict before a prompt or tool call runs.

Two features, used in the table below:

- **Invocation events**: one `AIInvocationObservedV1` per model response, pushed to `/ip/nhi/events/ai-invocations`. Metadata only unless `include_raw_content` is on.
- **Preflight**: a synchronous allow/deny from `/ip/nhi/events/ai-invocations/preflight` before the prompt or tool call proceeds. A deny blocks it and gives the model or user a reason.

## Implementations

| Implementation | Observes | Invocation events | Preflight | Deployment | Status | Observations |
| --- | --- | --- | --- | --- | --- | --- |
| [`bedrock/`](bedrock/README.md) | AWS Bedrock Model Invocation Logs | yes | no | Lambda, CloudFormation template | released, `bedrock-v0.1.6` | after the fact, by log subscription; no `bedrock-mantle` traffic |
| [`vertex/`](vertex/README.md) | GCP Vertex AI request-response logs (BigQuery) and Cloud Audit Logs | yes | no | Cloud Run + Cloud Scheduler + Firestore, Terraform module | released, `vertex-v0.1.12` | Gemini only in full; ~90-120 s latency; polling |
| [`anthropic/`](anthropic/README.md) | Claude Enterprise inference hooks and Compliance API | yes | yes | Cloud Run (Terraform) or any container host with a volume | released, `anthropic-v0.1.14` | no tokens or `stop_reason`; hook and compliance feeds join only on `tool_use` ids |
| [`codex/`](codex/README.md) | OpenAI Codex hooks (CLI and ChatGPT desktop app) and session rollouts | yes | yes | per-user local daemon, installed from PyPI by MDM | released, `codex-v0.1.4` | identity is claimed, not proven |
| [`portkey/`](portkey/README.md) | Portkey AI gateway guardrail webhooks, any provider behind the gateway | yes | yes, before the request and after the response | container image (GHCR), run anywhere the gateway can reach | not released yet, 0.1.0 | chat completions only; streamed answers are not judged after the response |
| [`agent-core/`](agent-core/README.md) | Amazon Bedrock AgentCore Harness hooks and Gateway interceptors | no | yes, per tool call and result | Lambda, CloudFormation template | released, `agentcore-v0.1.0` | tool calls only, no inference |

`shared/` is the internal library the forwarders reuse: event schema, HTTP sink, content hashing, base config, and the Anthropic, Converse and Gemini normalizers. It is not deployed on its own.

### Bedrock

Lambda in the customer's AWS account, triggered by a CloudWatch Logs subscription on the Bedrock Model Invocation Logging (MIL) log group. The handler normalizes each record, strips bodies, and pushes one invocation event. Only the push token is needed; SlashID resolves the org, connection and STS role chain on its side.

- **Status**: released (`bedrock-v0.1.6`). Each release attaches the Lambda zip and `cloudformation.yaml`.
- **Features**: invocation events with model, tokens, stop reason, tool catalog and tool uses, and accessed files (Converse attachments, `Read`-style tool results). Parses Anthropic, Converse, OpenAI Chat and Responses, Llama and DeepSeek bodies, streaming or not (`parsed_as`).
- **Not provided**: preflight. MIL is a log, so the forwarder sees a call only after it ran.
- **Failure handling**: no DLQ. After AWS's two async retries the Lambda `Errors` metric increments; alarm on it.
- **Limitations**: calls through `bedrock-mantle` are not logged by MIL; gpt-oss reasoning tokens are counted as output; Llama's raw prompt is one user message; some Chat Completions models write tool calls as text and are not recorded as tool uses; `s3://` attachments are hashed only if the Lambda role can read the object.

### Vertex

Cloud Run service driven by Cloud Scheduler. Each tick reads new request-response rows from per-region BigQuery datasets, joins identity from Cloud Audit Logs, normalizes each row, and pushes invocation events. A Firestore document holds the checkpoint and a lease so overlapping ticks do nothing.

- **Status**: released (`vertex-v0.1.12`). The Terraform module is consumed from the release tag and the image is published to GHCR.
- **Features**: invocation events for Gemini `generateContent` and `streamGenerateContent`, with tool declarations, tokens, and `fileData` attachments (hashed when the forwarder can read the bucket). Non-Google publishers and errored Google calls arrive as sparse `vertex-audit` events: identity, timestamp and model only.
- **Not provided**: preflight; the design is log polling.
- **Deployment**: the Terraform module creates the BigQuery datasets, enables request-response logging per publisher model, and provisions Cloud Run, Scheduler, Firestore and Secret Manager.
- **Limitations**: delivery is polling plus a 30 s identity buffer, so ~90-120 s from call to event; logging is enrolled per model (`observed_models`) and takes ~10 minutes to propagate; two callers of one model in the correlation window give partial identity; `locations/global` has no data residency; OpenAI-compat endpoint traffic carries no model and is not recorded; streaming `stop_reason` is a heuristic.

### Anthropic

Receives Claude Enterprise inference hook frames and answers allow or deny inline, and optionally polls the Compliance API. Neither pushes from the request path: both write to a pending store keyed by content, and a claim decides who pushes. One deployment, one push token.

- **Status**: released (`anthropic-v0.1.14`), image on GHCR, Terraform module in `deploy/terraform/`.
- **Features**: preflight on the hook (enforcing by default; `SLASHID_SHADOW_MODE` observes only; `SLASHID_VERDICT_FAIL_MODE` and a 3.5 s budget cover a failed check); invocation events from hook frames and, with `SLASHID_COMPLIANCE_KEY`, from the activity, chat-message and local-session feeds. Files are hashed from attachments and tool results, with Drive and SharePoint connector references.
- **Deployment**: Cloud Run with Firestore (`SLASHID_PLATFORM=gcp`), or `docker run` anywhere with a durable volume (default `local` platform, SQLite in `/data`, one replica). Anthropic must reach the webhook over public `https://` on 443 with a public CA certificate.
- **Limitations**: no token counts or `stop_reason` on any surface; a run with no tool call is recorded by the hook alone, and a run the hook never saw is never recorded; a denial is sticky and denies every later turn of the session; shadow and blocking modes are indistinguishable on the wire; Compliance API enablement is not retroactive; claude.ai extended research emits no frames; no tool or MCP-server inventory beyond tools used. The README lists what we would ask Anthropic for.

### Codex

A managed hook for OpenAI Codex, the CLI and the ChatGPT desktop app. Each hook runs a thin client that forwards to a per-user daemon on `127.0.0.1`, which answers preflight on a warm connection, reads the session rollouts under `~/.codex/sessions`, and pushes events from a worker thread. The daemon starts on first use and exits after 10 idle minutes.

- **Status**: released (`codex-v0.1.4`), one wheel on PyPI (`slashid-ai-forwarder-codex`) that carries the shared core.
- **Features**: preflight on `UserPromptSubmit` and `PreToolUse` (file names and hashes, model, tool and MCP server names; never prompt text, arguments or paths); invocation events per model response; `verdict_fail_mode` (default `deny`) and a 4 s verdict budget.
- **Deployment**: MDM installs the package where users cannot modify it (uv, pipx or pip), plus a read-only config and token file and Codex's managed `requirements.toml`. Windows, macOS and Linux paths are in the README. Setup guide: the SlashID console docs.
- **Limitations**: a hook Codex cannot start is a nonblocking failure and the action goes through unchecked; reads are checked only for simple shell reads (`cat`, `sed`, `head`, `tail`, `nl`) and `view_image`; the token is per connection, so a user can send any `user_id`; daemon lifetime and the desktop bundle paths on macOS and Windows are unverified.

### Portkey

A webhook service Portkey calls as a guardrail, before the request leaves the gateway and after the model answers, for any provider behind it. The service asks SlashID and returns the verdict; the application names the person (`_user`) and the agent (`agent`) in Portkey's `metadata`.

- **Status**: 0.1.0, merged but not released: no `portkey-vX.Y.Z` tag yet.
- **Features**: preflight before the request (the caller, the model and the new part of the conversation: prompt, tool results, inline files) and after the response (the tool calls the model asked for, with arguments); a deny makes Portkey return 446. After the response it also records the invocation (model, tokens, tools, files). `SLASHID_VERDICT_FAIL_MODE`, `SLASHID_SHADOW_MODE` and a 2.5 s budget apply.
- **Deployment**: the container image `ghcr.io/slashid/slashid-ai-forwarder-portkey:<version>`, run where the gateway can reach it (a self-hosted gateway on a private network lists the host in `TRUSTED_CUSTOM_HOSTS`). The Event streaming token travels with each webhook call, so one deployment serves any number of connections.
- **Limitations**: only chat completions are judged, not `/v1/messages`, legacy completions or embeddings; a request whose metadata names neither a user nor an agent takes the fail mode; Portkey gives a webhook no response body for a streamed answer, so its tool calls are not judged and its tokens are not recorded; a webhook call that fails outright is Portkey's to decide.

### AgentCore

One Lambda that checks every tool call of an Amazon Bedrock AgentCore agent, and its result, with SlashID. AgentCore Harness lifecycle hooks (`before_tool_call`, `after_tool_call`) and Gateway interceptors (REQUEST and RESPONSE on `tools/call`) invoke it, and it answers allow or deny.

- **Status**: released (`agentcore-v0.1.0`), with the Lambda zip and `cloudformation.yaml` on the GitHub release. Tested live against a real Google Drive MCP through a Gateway; unconfirmed items are listed in its README.
- **Features**: preflight only. Nothing is pushed: no invocation events. The check sends tool names, ids, identity and file references (Drive, Microsoft 365, SharePoint connector tools), never prompts, arguments or results. `FailMode`, `ShadowMode` and a budget (default 3.5 s) apply. A result check can replace a Gateway tool result with `Content withheld: ...`.
- **Deployment**: Lambda from a CloudFormation template, deployed in the region of the Harnesses and Gateways it serves. Each Harness and Gateway execution role is listed in the stack so it may invoke the function; hooks and interceptors are attached with the AWS CLI. A Harness's hooks are removed by every `agentcore deploy`.
- **Limitations**: a Runtime agent is covered only for tools behind a Gateway; a Harness hook is capped at 64 KiB, so a larger result is checked without its file references; an `after_tool_call` deny stops the run but the result stays in the session, while a Gateway RESPONSE deny does withhold it; a Gateway has no failure mode of its own; a Harness with Gateway tools makes four serial checks per call (~170-300 ms each warm, ~1 s cold); the Harness id on the Gateway path is as weak as the role, and a JWT-authorized Gateway cannot verify it at all.

## Development

Requirements: `uv`, `pre-commit`.

```bash
uv sync                                    # install workspace deps + dev tools
uv run pre-commit install                  # set up hooks

# per-subproject (tool config lives in each subproject's pyproject.toml)
(cd bedrock   && uv run pytest)            # Bedrock tests
(cd vertex    && uv run pytest)            # Vertex tests
(cd anthropic && uv run pytest)            # Anthropic tests
(cd portkey   && uv run pytest)            # Portkey tests
(cd agent-core && uv run pytest)           # AgentCore tests
(cd codex     && uv run pytest)            # Codex tests
(cd shared    && uv run pytest)            # Shared library tests
(cd bedrock   && uv run ruff check .)      # Bedrock lint
(cd vertex    && uv run ruff check .)      # Vertex lint
(cd anthropic && uv run ruff check .)      # Anthropic lint
(cd portkey   && uv run ruff check .)      # Portkey lint
(cd agent-core && uv run ruff check .)     # AgentCore lint
(cd codex     && uv run ruff check .)      # Codex lint
(cd shared    && uv run ruff check .)      # Shared lint
(cd bedrock   && uv run ty check)          # Bedrock type-check
(cd vertex    && uv run ty check)          # Vertex type-check
(cd anthropic && uv run ty check)          # Anthropic type-check
(cd portkey   && uv run ty check)          # Portkey type-check
(cd agent-core && uv run ty check)         # AgentCore type-check
(cd codex     && uv run ty check)          # Codex type-check
(cd shared    && uv run ty check)          # Shared type-check
```

## Releases

Tagged releases are per-component with a component prefix:

- `bedrock-vX.Y.Z` → publishes Lambda zip + CloudFormation template to GitHub Releases (see `bedrock/README.md`).
- `vertex-vX.Y.Z` → publishes the service container image to GHCR; the Terraform module is consumed from the tag (see `vertex/README.md`).
- `anthropic-vX.Y.Z` → publishes the receiver container image to GHCR; the Terraform module is consumed from the tag (see `anthropic/README.md`).
- `portkey-vX.Y.Z` → publishes the webhook container image to GHCR (see `portkey/README.md`).
- `agentcore-vX.Y.Z` → publishes Lambda zip + CloudFormation template to GitHub Releases (see `agent-core/README.md`).
- `codex-vX.Y.Z` → publishes the wheel to PyPI and a GitHub Release (see `codex/README.md`).

The version in the tag must match the `version` field in the component's `pyproject.toml`. The release workflow enforces this.
