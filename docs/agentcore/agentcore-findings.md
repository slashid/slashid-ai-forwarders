# Amazon Bedrock AgentCore: what it is, what it exposes, what we tested

**Date:** 2026-10-30
**Purpose:** a digestible account of AgentCore and every surface a SlashID forwarder could use, with what each one actually carries, built from live tests in a scratch AWS account. This is research, not a design: the last section lists the questions the design must answer.
**Status of evidence:** every statement is tagged.

| Tag | Meaning |
| --- | --- |
| ✅ | Observed in a live test we ran (payloads captured) |
| 📖 | From AWS or Google documentation only, not exercised |
| ⛔ | Attempted and blocked (reason given) |

Account ids, file ids and secrets are generalized (`<account>`, `<tagged-file-id>`). The test account is `us-east-2`; everything we created is prefixed `agentdemo-`.

---

## 1. Summary

1. **AgentCore is a platform for hosting and operating agents, not a model API.** Models are still called on Bedrock or any other provider. Our existing `bedrock/` forwarder covers model calls that Bedrock logs; AgentCore adds *tool calls*, which Bedrock never sees. ✅
2. **An agent is a loop** (model, tool, model, ...). AgentCore runs it in two ways: **Runtime** (the customer's own code in a microVM) and **Harness** (a managed loop, configured not coded). ✅
3. **Tool calls are observable at two enforcement points that need no change to the agent:** Harness lifecycle hooks (Harness only) and Gateway interceptors (any agent whose tools go through a Gateway). Both can deny a call before it runs, with the reason reaching the model. ✅
4. **AgentCore Policy (Cedar) is a third enforcement point** on the same Gateway, with its own deny format and its own decision log. It runs *after* our REQUEST interceptor and *before* the target. ✅
5. **Identity is weak.** Hooks carry the agent (`harnessArn`) but no caller. The Gateway carries a caller, but for agents it is a *role*, not a person. A human appears only when a JWT is presented to a JWT-authorized Gateway. The Harness `--user-id` is never propagated. ✅
6. **Model-call detail is not available from hooks or interceptors on the default path.** It comes from traces (optional, format not stable), Bedrock MIL (Converse only), or by routing model traffic through a Gateway (changes the agent). ✅
7. **Harness hooks are fragile to operate:** they cannot be declared in the project file, and every `agentcore deploy` silently removes them. ✅ Gateway interceptors were steadier: they survived all six deploys, but only because our gateways are outside the CLI project; the CLI cannot declare interceptors either, and an `UpdateGateway` that omits them clears them (tested), so any tool that updates a gateway without resending them removes them silently.
8. **Google's hosted Drive MCP server rejected every real call** even after Preview-program approval, correct scopes, enabled APIs and a project role; we could not resolve it. A stand-in with Google's real tool names, schemas and the real file bytes covers the testing. ⛔
9. **Provider independence holds for tool calls on both surfaces:** Bedrock (Converse and OpenAI-format), OpenAI, Anthropic and Gemini all produce identical **Harness hook** payloads ✅ (compared per provider). The **Gateway interceptor** saw one MCP shape for the harness's 25 tool calls across those runs (a request, a result, one error), with only the model-chosen arguments varying ✅ (structural check; events cannot be attributed to a provider from the log itself). Model calls themselves are not covered by either surface.

---

## 2. What AgentCore is

### 2.1 Components

| Component | What it is | Relevance to us |
| --- | --- | --- |
| **Runtime** | Serverless host for agent code (any framework); one isolated microVM per session; HTTP endpoint; has a workload identity and an execution role. 📖✅ | The "code agent" case. No lifecycle hooks exist on it. |
| **Harness** | A managed agent loop: you declare model, system prompt, tools, hooks; AgentCore runs the loop. Backed by Runtime. ✅ | The "no code" case. Has lifecycle hooks. |
| **Gateway** | One MCP endpoint that fronts APIs, Lambdas and other MCP servers as tools ("targets"); inbound auth IAM or JWT; can also proxy model traffic (inference targets). ✅ | The main tool-call choke point. Hosts interceptors and Policy. |
| **Gateway interceptors** | Up to one REQUEST and one RESPONSE Lambda per Gateway, run around every call. ✅ | Our primary hook surface for Gateway tools. |
| **Policy** | Cedar rules evaluated by the Gateway for each tool call; default deny. ✅ | AWS's own allow/deny engine; coexists with our preflight. |
| **Identity** | Token vault for OAuth and API-key credentials; also workload identities. ✅ | Needed for non-Bedrock model keys and 3-legged OAuth tools. |
| **Memory, Browser, Code Interpreter, Registry, Evaluations, Payments** | Other services. 📖 | Not tested; not needed for tool-call collection. |
| **Observability** | OTEL traces, logs and metrics in CloudWatch/X-Ray. ✅ | Optional enrichment source. |

### 2.2 What an "agent" is

A program that calls a model with a prompt and a list of tools. If the model asks for a tool, the program runs it, appends the result and calls the model again, until the model answers without a tool request. The model never executes anything; **the program does**, so the program (or the platform around it) is where a call can be observed or blocked.

```
while True:
    reply = model(messages, tools)          # inference: Bedrock, OpenAI, Gemini, ...
    if no tool call in reply: break
    result = run_tool(reply.tool_call)      # a function, an MCP server, an API
    messages += [reply, result]
```

**From the outside, an agent is an RPC.** The caller sends one request (`InvokeHarness` or `InvokeAgentRuntime`: the user's message plus a session id), the loop above runs inside the call, and the final answer is the response. It differs from an ordinary RPC in three ways:

- **The response is a stream, not one value:** text deltas, tool-use events, a final `stopReason` and token usage (`contentBlockDelta`, `messageStop`, `metadata` and `hookEvent` events in our runs) ✅.
- **One outer call contains a tree of inner calls, with side effects.** Inside it the agent makes many model calls and tool calls (MCP servers, APIs), and the tools act on the world. Every surface in this document observes one of those *inner* calls, not the outer one.
- **It is stateful and can return early.** Calls with the same session id continue one conversation 📖 (our demo used a new session per call). A Harness can return mid-loop with `stopReason: tool_use` to hand an inline function to the caller, who answers in a follow-up call 📖; hooks can stop a run (`hook_stopped`) 📖; a failed model call returns `interrupted` ✅.

An agent can be **interactive** (a front end calls its endpoint) or **autonomous** (a schedule, an event or another agent calls it). AgentCore has no chat UI of its own; a person is present only if someone builds a caller. This is why identity is mostly a *workload* identity.

**Example: an interactive agent.** A "Drive assistant" in a company chat window, built on a Harness or a Runtime agent:

```
Paulo, in the browser:  "Find banana-bread.md and tell me the bake time."
        │
        ▼
  chat web app          signs Paulo in, holds his token, keeps one session id per conversation
        │  InvokeHarness / InvokeAgentRuntime   (sessionId = this chat; Paulo's token if the tools need it)
        ▼
  the agent loop        model ─▶ search_files("banana-bread.md") ─▶ model ─▶ read_file_content(<id>) ─▶ model
        │
        ▼  streamed answer
  chat web app          shows "Bake it for 55-65 minutes" to Paulo
        │
Paulo:  "And how many bananas?"     ← next turn, same session id, the agent remembers the file
```

What is different from an autonomous agent (say, a nightly job that triages new tickets and nobody watches):

- **A person is waiting**, so the app can show things to them: a consent link when a tool asks for the user's own OAuth consent (section 4.3), or a denial such as "I couldn't read that note; it is tagged sensitive" (the model relays the deny reason, as in our tests).
- **A user identity exists**, in the web app. It reaches AgentCore only if the app passes a token on (a JWT to a JWT-authorized Gateway); otherwise the platform sees just the agent's role and session.
- **Sessions span turns**: one session id per conversation, many model/tool rounds per turn, and the same agent serves many users, each with their own session.
- **Our demo calls** (`agentcore invoke`, `invoke_harness`) played the role of the web app: one prompt in, the loop runs, the answer streams back.


### 2.3 Relationship to Bedrock

| | Bedrock | AgentCore |
| --- | --- | --- |
| What | Model API (Converse, InvokeModel, OpenAI-compatible endpoints) | Hosting and operating agents |
| Logs | Model Invocation Logging (MIL) to CloudWatch/S3 | Traces, Gateway logs, hooks, Policy logs |
| Covered by `bedrock/` forwarder | Yes (Converse/InvokeModel) | No |

Agents on AgentCore may use **non-Bedrock models** (OpenAI, Gemini, Anthropic API). Those calls never touch MIL. ✅

### 2.4 Where each surface sits

```
                        HARNESS (managed loop)                      RUNTIME (customer code)
user/app ──invoke──▶  before_invocation ◀─S1                  user/app ──invoke──▶ agent code
                       │                                                            │
                       ▼                                                            ▼
                     model call ──▶ Bedrock / OpenAI / Gemini / Anthropic        model call ──▶ same providers
                       │              (MIL sees Converse only: S8;                    │
                       │               apiBase can route via Gateway: S4)             │
                     tool call                                                    tool call
                       │                                                            │
        before_tool_call ◀─S1                                                       │
                       │                                                            │
                       └────────────▶ GATEWAY ◀─────────────────────────────────────┘
                                       │  REQUEST interceptor ◀─S2   (can deny / rewrite)
                                       │  Policy (Cedar)      ◀─S5   (can deny; filters tools/list)
                                       │  target: Lambda | OpenAPI | MCP server | inference
                                       │  RESPONSE interceptor ◀─S2  (sees result / policy error)
                                       │  application logs    ◀─S6
                       after_tool_call ◀─S1 (Harness)
                       after_invocation ◀─S1
        traces / OTEL logs ◀─S7 (both shapes)
```

---

## 3. The surfaces

Each entry lists what it sees, what it can do, how identity appears, limits, and the evidence.

### S1. Harness lifecycle hooks ✅

- **What:** Lambda (decides), SNS or EventBridge (notify only) called at four points of the Harness loop: `before_invocation`, `before_tool_call`, `after_tool_call`, `after_invocation`. **There is no before/after-model hook.** Up to 20 per harness.
- **Attach:** only through `CreateHarness/UpdateHarness` (`hooks`). The project file (`harness.yaml`) rejects the key; attach them with `--hooks` on `aws bedrock-agentcore-control create-harness` / `update-harness` (verified with CLI 2.39.3) ✅. **Every `agentcore deploy` removes them.** `UpdateHarness` replaces the whole list, so attaching to an existing harness needs read-merge-write.
- **Payload** (identical for every model provider; 4 shapes):

| Event | `context` carries |
| --- | --- |
| `before_invocation` | `messageCount`, `messages` (only the new user messages of this request) |
| `before_tool_call` | `toolName`, `toolType` (`builtin`, `agentcore_gateway`, ...), `toolInput`, `toolUseId` |
| `after_tool_call` | `toolName`, `toolType`, `toolUseId`, `toolResult{status, content[{text}], isError?}`; **no `toolInput`** (none of 19 captured events had one), so the arguments must come from the earlier `before_tool_call` of the same `toolUseId` |
| `after_invocation` | `stopReason`, `usage{inputTokens, outputTokens, totalTokens, cacheReadInputTokens?}`: totals for the whole invocation, i.e. one `InvokeHarness` request. It fires **once per request, not per model call**: each of 21 captured sessions had exactly one `before_invocation` and one `after_invocation`, whether the run made 0, 1 or 2 tool calls. A mid-loop return (`stopReason: tool_use` for an inline function) ends an invocation and the follow-up request is a new one 📖 |

  Every event also has `hookEventId, name, event, harnessArn, sessionId` and an **undocumented** `headers{traceparent, baggage(harness.id, session.id, endpoint qualifier), X-Amzn-Trace-Id}`. Context is capped at 64 KiB. A 90 KB `shell` output arrived in `after_tool_call` with the whole `toolResult` replaced by `{truncated: true, preview: "<first ~64 KiB of the result's JSON text, cut mid-string>"}` ✅: `status`, `isError` and `content` are only inside the preview text, while `toolUseId` stays in the context. Whether a large `toolInput` is truncated the same way is untested 📖 (the demo model, Haiku 5.5, would not write a 64 KiB command).
- **Can act:** `before_tool_call` deny skips the call; the model receives the reason as the tool result, tells the user and does not work around it ✅. `after_tool_call` deny stops the run (`stopReason: hook_stopped`, no answer) ✅, **but the result stays in the session: on the next request in the same session the model quoted it** ✅. It does not keep the content from the model. `failureMode` allow|deny (default deny), timeout 1-900 s 📖.
- **Absent:** the caller / `--user-id`, any principal, the model id, per-call tokens, system prompt and history, assistant text.
- **Quirks:** `after_tool_call` fires even after a denied `before_tool_call`, with `status:"error"` and the deny text, and nothing marks who denied. A failed model call still fires `before_invocation` and `after_invocation` with `stopReason:"interrupted"` and zero tokens.
- **Inline functions span two invocations** ✅: request 1 fires `before_invocation`, `before_tool_call` and `after_invocation` (`stopReason: tool_use`) and **no** `after_tool_call`; the caller runs the function and sends a follow-up request with the same session id, which fires `before_invocation` (its message is the `toolResult`), `after_tool_call` (with the client-supplied result) and `after_invocation`, and **no** `before_tool_call`. A consumer must pair the two halves by `toolUseId` across invocations. The `after_tool_call` confirms only that the harness accepted the caller's result, not that the caller did the work 📖.

### S2. Gateway interceptors, MCP shape ✅

- **What:** the Gateway invokes the interceptor Lambda for **every MCP message** (initialize, notifications, `tools/list`, `tools/call`), once before (REQUEST) and once after (RESPONSE) the target.
- **Payload (`event.mcp`):** `gatewayRequest{path, httpMethod, headers (only with passRequestHeaders), body (parsed JSON-RPC), context.identity.awsPrincipalArn}`; `gatewayResponse` is **null on REQUEST**; `rawGatewayRequest`. Lambda client context adds `GATEWAY_ARN, GATEWAY_ACCOUNT_ID, REQUEST_ID, SOURCE_IP`. Tool names are `<target>___<tool>`. `REQUEST_ID` equals the `bedrockAgentCoreAwsRequestId` the tool Lambda receives, so the layers join.
- **What it gives beyond tool calls:** the `tools/list` RESPONSE is the whole catalog (name, description, inputSchema; Google adds annotations and outputSchema); `initialize` RESPONSE has server info and capabilities. These map onto our `AITool` and `AIToolServer` event structures.
- **Catalog size.** A Gateway is one MCP endpoint aggregating N *targets* (MCP servers, Lambdas with declared schemas, OpenAPI or Smithy specs, API Gateway stages, connectors; inference targets add no tools), and `tools/list` is the union, prefixed `<target>___`. We saw 7 tools / 7 KB for two small targets ✅ and **8 tools / 39 KB from the real Google Drive server alone** (`search_files` is 7.4 KB) ✅, with no pagination cursor. Every model request carries these definitions (they count as input tokens), so large catalogs are costly. Ways to shrink them: Policy hides unpermitted tools (7 became 4) ✅, an interceptor can filter 📖, Harness `allowedTools` patterns, Gateway semantic search 📖, and a setting that disables list pagination 📖.
- **Can act:** a REQUEST interceptor that returns `transformedGatewayResponse` short-circuits: the tool is not called, **the RESPONSE interceptor still runs**. A body `{"result":{"isError":true,"content":[{"type":"text","text":reason}]}}` reaches the model verbatim; a JSON-RPC `error` body is wrapped as "Tool execution failed: <message>". A RESPONSE interceptor can rewrite results ✅ (we replaced the `read_file_content` result of the tagged file with a notice and the model saw only the notice) or hide tools from `tools/list` 📖 (not tested). **The rewrite must cover every result that carries the file, not only the read tools:** `search_files` returned a `contentSnippet` of the tagged file and the model quoted it even with the read redacted ✅.
- **Persistence:** the interceptors on all three of our gateways survived six `agentcore deploy` runs ✅, but those gateways are not declared in the CLI project, so the CLI never touched them. The CLI has no interceptor setting (no flag in `add gateway`, no key in its project schema) ✅. **`UpdateGateway` replaces them as a whole**: a call that omitted `interceptorConfigurations` left the gateway with none (tested on the inference gateway, then restored) ✅. Any tool that updates a gateway without resending them, very likely including the CLI when it updates a gateway it manages (for example to attach a policy engine), would remove them silently; that CLI case is untested.
- **Limits:** one REQUEST and one RESPONSE interceptor per Gateway, so a customer's existing interceptor and ours conflict. Lambda payload cap 6 MB. With response streaming on, the RESPONSE interceptor is called per event 📖 (we ran with streaming off).

### S3. Gateway interceptors, HTTP shape ✅

- **Used for:** inference targets ✅ and HTTP targets such as an AgentCore Runtime behind the Gateway 📖 (only inference targets were exercised). Payload `event.http.gatewayRequest{path, httpMethod, headers, body (base64)}`.
- **Differences from S2:** `context` is absent, so **no principal** (only the SigV4 `Authorization` header or a JWT); the RESPONSE event has **`gatewayRequest: null`** (pair request and response by `REQUEST_ID`); streaming responses arrive **buffered as one body** (SSE text or `application/vnd.amazon.eventstream`).
- **Pass-through:** return `{"interceptorOutputVersion":"1.0","http":{}}`.

### S4. Gateway as a model proxy ✅ (optional by nature)

- **What:** inference targets (`bedrock-runtime`, `bedrock-mantle`, `openai`, `anthropic`, or custom provider) route `/inference/...` by the request's `model`. An interceptor then sees **provider-native bodies**: OpenAI Chat Completions or Responses, Anthropic, Bedrock InvokeModel, including all tool definitions and the full streamed answer.
- **The catch:** it sees only traffic that is *sent to it*. The agent's model client must be repointed.
  - Harness, by configuration: `openAiModelConfig.apiBase` / `liteLlmModelConfig.apiBase`. We ran this successfully, but the Harness sends its stored "API key" as a bearer token, so the Gateway had to be JWT-authorized and the stored value a valid JWT (a one-hour Google ID token in the test). The real OpenAI key lived in the Gateway target. The stored value is static, so it expires.
  - Runtime: code or environment (`base_url`).
- **Observed:** 14 events for two harness runs: 8 tools, `stream:true`, SSE responses of 10 to 52 KB, and the JWT claims of whoever the stored token represented.
- **Judgment:** a deployment architecture a customer would choose ("all LLM traffic through the Gateway"), not something a forwarder can switch on. Not part of the default path.

### S5. AgentCore Policy (Cedar) ✅

- **Model:** a policy engine holds Cedar policies; attached to a Gateway in `LOG_ONLY` or `ENFORCE`. Each tool is a Cedar action `AgentCore::Action::"<target>___<tool>"`, the resource is `AgentCore::Gateway::"<gateway arn>"`, tool arguments are `context.input.*`, the principal is `AgentCore::IamEntity` (id = caller ARN) or `AgentCore::OAuthUser` (JWT subject, claims as tags). Default deny; forbid wins. Dogwood (a Cedar superset) adds session-history (temporal) and guardrail conditions 📖.
- **Tested policies:** permits for four Drive tools, plus `forbid read_file_content when context.input.fileId == <tagged-file-id>`.
- **ENFORCE results:** the forbid denied the tagged file; `download_file_content` and the demo tool, having no permit, were denied by default; allowed calls passed.
  - **Denial shape:** JSON-RPC error `-32002`, "Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to <policy>]" or "[No policy applies to the request (denied by default).]".
  - **`tools/list` is filtered** to permitted tools (7 became 4).
  - **`LOG_ONLY`** records the same verdicts and lets calls through.
- **Order:** REQUEST interceptor, then Policy, then target, then RESPONSE interceptor. Our REQUEST interceptor sees calls Policy later denies; our RESPONSE interceptor sees the `-32002` error and the already-filtered catalog.
- **Operational facts:** the Gateway role needs `GetPolicyEngine`, `AuthorizeAction`, `PartiallyAuthorizeActions`; the policy creator needs `InvokeGateway`; the strict default validation rejected plain permits as "overly permissive" and even a conditional forbid as "overly restrictive", so we used schema-only validation. Evaluation added about **36-39 ms** per call (from the decision log).
- **Relationship to us:** Policy is static (ids and conditions written into policy text). It cannot consult SlashID's live graph of tagged files. Complementary, not a replacement.

### S6. Gateway application logs ✅

- **Off by default.** Enabled with CloudWatch vended-log delivery (`APPLICATION_LOGS`) to a log group.
- **Contents** (per `tools/call`): "Started processing request" with the JSON-RPC request body, "Received request for tools/call", "Executing tool X from target Y", the response body, and, with Policy attached, a record `policy{decision ALLOW|DENY, reason, determiningPolicies, principal{entityType,entityId}, latencyMs}`.
- **Use:** a passive, no-Lambda source of tool-call bodies and Policy verdicts. Same data volume as the traffic.

### S7. Runtime and Harness traces and OTEL logs ✅

- **Runtime OTEL logs** (agent log group, stream `otel-rt-logs`): per Bedrock call, `gen_ai.system/user/assistant/tool.message` and `gen_ai.choice` events with **full prompts, tool arguments and results**, `finish_reason`, keyed by trace and span id. On by default with the CLI template (ADOT is a dependency). No model id or tokens here.
- **X-Ray spans** (needs CloudWatch Transaction Search): `invoke_agent > execute_event_loop_cycle > chat > BedrockRuntime` and `execute_tool <name>`, with `gen_ai.request.model`, `gen_ai.usage.input/output_tokens` per model call, finish reasons, tool name, id, status and schema (not inputs or outputs). Retrieved with `xray batch-get-traces`; `FilterLogEvents` on `aws/spans` returned nothing.
- **Harness:** traces, logs and metrics are automatic 📖.
- **Judgment (yours):** attribute names are OTEL gen_ai conventions emitted by Strands; not stable enough to build on. Treat as optional enrichment.
- **Side effect:** the first CLI deploy turned on CloudWatch Transaction Search account-wide (new log groups and a resource policy).

### S8. Bedrock Model Invocation Logging (existing path) ✅

- Logs Bedrock Converse and InvokeModel calls with bodies. **Does not log** Bedrock-Mantle (OpenAI-format) calls, nor OpenAI, Anthropic or Gemini direct calls. In our run, MIL recorded the Converse (Haiku) calls and none of the Mantle (`gpt-oss`) calls.

### S9. Not tested

- **Strands in-process hooks** (BeforeModelCall, BeforeToolCall, etc.): run inside the customer's agent code, so out of scope. 📖
- **CloudTrail data events** for `InvokeAgentRuntime`: this account's trail records management events and S3 data events only; enabling them would modify the existing trail. 📖
- **Gateway metrics** and **Gateway tracing.** 📖
- **Interceptor response streaming mode**, **HTTP-target interceptors for Runtime targets**, **Policy temporal and guardrail conditions.** 📖

---

## 4. What information is available where

✅ = observed, ⚠ = partial or indirect, – = not available, 📖 = documented only.

| Information | S1 - Harness hooks | S2 - Gateway interceptors, MCP | S3 - Gateway interceptors, HTTP | S5 Policy log | S6 Gateway log | S7 Traces / OTEL | S8 - Bedrock MIL |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Agent identity | ⚠ `harnessArn` (the harness resource only, see section 6) | ⚠ role ARN; Harness adds `harness.id` | ⚠ SigV4 key id only | ⚠ caller ARN | ⚠ caller | ✅ runtime endpoint ARN, service name | ⚠ caller ARN (no agent) |
| Caller / human | – | ⚠ JWT claims on a JWT gateway; else role/user ARN | ⚠ JWT only | ✅ principal (ARN or OAuth subject) | ✅ via policy record | – | ⚠ IAM caller |
| Session id | ✅ `sessionId`, baggage | ✅ baggage `session.id` | – | – | – | ✅ | – |
| Trace id | ✅ `traceparent` | ✅ `traceparent` | ⚠ header only | ✅ `traceId` | ✅ | ✅ | – |
| Tool name | ✅ | ✅ | – | ✅ action | ✅ | ✅ | ⚠ in body |
| Tool arguments | ✅ | ✅ | – | ✅ `context.input` (for rules) | ✅ | ⚠ in OTEL logs | ⚠ in body |
| Tool result | ✅ `after_tool_call` | ✅ RESPONSE | – | – | ✅ | ⚠ OTEL logs | – |
| File ref (Drive `fileId`) | ✅ from args | ✅ from args | – | ✅ rules on args | ✅ | ⚠ | – |
| File content hash | ✅ only from a `download_file_content` result | ✅ same | – | – | ✅ same | – | – |
| Tool catalog (`available_tools`) | – | ✅ `tools/list` RESPONSE | – | ⚠ filtered catalog | ✅ | ⚠ `gen_ai.agent.tools` | – |
| Model id | – | – | ✅ in proxy bodies | – | – | ✅ | ✅ |
| Prompts / answers | ⚠ new user messages only | – | ✅ in proxy bodies | – | – | ✅ OTEL logs | ✅ Converse only |
| Tokens | ⚠ per run | – | ✅ in proxy bodies | – | – | ✅ per call | ✅ per call |
| Can deny before the call | ✅ `before_tool_call` | ✅ REQUEST | ✅ REQUEST | ✅ ENFORCE | – | – | – |
| Can rewrite result / hide tools | – | ✅ RESPONSE | ✅ RESPONSE | ⚠ hides tools | – | – | – |
| Needs no change to the agent | ✅ | ✅ if tools use the Gateway | ❌ model client must be repointed | ✅ | ✅ | ✅ | ✅ |
| Covers Runtime agents | – | ✅ if tools use the Gateway | ✅ if repointed | ✅ same | ✅ same | ✅ | ✅ |
| Covers non-Bedrock models | ✅ (tool calls) | ✅ (tool calls) | ✅ | ✅ | ✅ | ✅ | – |

### 4.1 Agent shape by surface

| | Harness | Runtime (code) |
| --- | --- | --- |
| Hooks (S1) | ✅ | – none exist |
| Gateway interceptor, Policy, Gateway logs | ✅ for tools behind a Gateway | ✅ for tools behind a Gateway |
| Tools **not** behind a Gateway (built-in shell, local functions, direct MCP/API calls) | hooks only (`toolType` `builtin`, `remote_mcp`, ...) | **invisible to hooks, interceptors, Policy and Gateway logs**; only traces and OTEL logs show them (local tools appear as `execute_tool` spans) |
| Identity of the agent | `harnessArn`, `harness.id` | execution role (+ session id via baggage) |
| Trace context to the Gateway | `traceparent` + `baggage` incl. `harness.id`, `session.id` | `traceparent` + `baggage` incl. `session.id` (ADOT, no code of ours) |

### 4.2 Which tools can each surface see

Two questions decide it: is the tool defined *inside* the agent (internal) or a remote service (external), and, if external, does it go through a Gateway?

| Tool kind | Example | Harness hooks | Gateway interceptor, Policy, Gateway logs | Traces |
| --- | --- | --- | --- | --- |
| Internal, Harness built-in | `shell`, `file_operations` | ✅ `toolType: builtin` | no | 📖 |
| Internal, Runtime in-code function | `add_numbers` | no hooks on Runtime | no | ✅ `execute_tool` span |
| Harness inline function (runs in the caller) | `get_weather` | ✅ `inline_function`: `before_tool_call` in the first request, `after_tool_call` in the follow-up | no | |
| External via a Gateway target | `gdrive___search_files` | ✅ `agentcore_gateway` | ✅ | ✅ |
| External bypassing the Gateway | Harness `remote_mcp` (a public MCP server by URL), or a Runtime agent calling an API directly | ✅ Harness `remote_mcp` only | no | Runtime ✅ if instrumented |
| AgentCore managed tools | Browser, Code Interpreter | 📖 Harness | no | |

Gateway-side surfaces see only Gateway-routed tools. Hooks sit in the Harness loop and see every tool type, but exist only for Harness.

**Custom tools on a Harness.** A Harness holds no code of its own, so it has no equivalent of a Runtime agent's in-process `@tool` function. It has default tools (`shell`, `file_operations`, enabled unless `allowedTools` restricts them), AgentCore-managed tools (Browser, Code Interpreter), and custom tools that arrive from outside the loop: a **Gateway target** such as your own Lambda or OpenAPI spec ✅ (our toy tool and the Drive stand-in), your own **remote MCP server** by URL ✅ (tool names are `<server>_<tool>` with one underscore, e.g. `deepwiki_read_wiki_structure`), an **inline function** executed by the caller, which receives the call and returns the result in a follow-up request ✅, or code in a **custom container image** that the model runs through `shell` 📖. Hooks label these `builtin`, `agentcore_gateway`, `remote_mcp` and so on (`toolType`), which is how a hook consumer can tell a custom tool from a default one.

When a Harness uses Gateway tools, **hooks and the interceptor both see the same call** and join by trace id (same `traceparent`). A design that uses both must de-duplicate.

**Order of events for one call** ✅ (28 of 28 calls in the captures, ordered by millisecond and joined on trace id):
`Harness before_tool_call` → `Gateway REQUEST` → *(Policy, when attached)* → target → `Gateway RESPONSE` → `Harness after_tool_call`. In one measured call the four events were at +0, +79, +174 and +198 ms. A deny shortens it: a Harness `before_tool_call` deny means the Gateway is never reached; a Gateway REQUEST or Policy deny skips the target but the RESPONSE and the Harness `after_tool_call` still fire, carrying the deny text.


### 4.3 Tools that need the user's own consent (3-legged OAuth) ✅

Some external tools act with the *user's* credentials (Drive was the example): a Gateway target configured with the OAuth authorization-code grant. They behave differently from every other tool kind.

| Aspect | What we saw |
| --- | --- |
| Where they live | A Gateway target (MCP server or OpenAPI) with outbound OAuth, backed by a credential provider in AgentCore Identity |
| Gateway authorizer | **JWT only.** An IAM-authorized Gateway rejects the target: "3LO Auth is not supported when gateway authorizer type is AWS_IAM" |
| Who can call | Whoever presents a valid JWT for the Gateway. A Runtime agent must forward the user's token. A Harness can use a Gateway tool with `outboundAuth.oauth` 📖; **we only ran the Harness against the IAM gateway, so Harness plus 3-legged tools is untested** |
| First call with no stored token | A JSON-RPC **error `-32042`** ("This request requires more information") carrying a consent URL; needs MCP `2025-11-25` on the Gateway and the `MCP-Protocol-Version` header. A person must open the URL and consent; an **autonomous agent cannot complete it**, so such calls fail until someone does |
| Consent is per user, provider and scope set | Changing the scopes forced a new consent. The target's own admin sync consent is separate (a synthetic user) |
| What a RESPONSE interceptor saw (62 events) | The `-32042` error twice; and tool failures that arrive as an ordinary `isError` **result** with plain text: protocol version too old, outbound-token fetch not authorized (an IAM gap), and Google's own refusal ("The caller does not have permission", five times) |
| Identity on these events | The inbound JWT claims on every event (`iss, sub, email, hd, aud, azp`): the person (or machine) whose token was presented |
| Outbound credential | **Not present in any payload we captured** (no Google access token, no response headers). It stays in the token vault |
| Policy principal | `AgentCore::OAuthUser` with claims as tags 📖; not tested on this Gateway |
| Never seen working | Every real Drive call was refused by Google, so we never observed a *successful* 3-legged tool result; the stand-in is an IAM-authorized Lambda target |

What this means for the surfaces: the only place a human identity appears is these JWT gateways; a consent prompt is a *request for consent*, not a failed or denied tool call, and arrives as a different error shape than the other failures; and the forwarder never needs, and never sees, the user's outbound credential.

---

## 5. Model providers tested through the Harness ✅

Each run gave the model the same task: find `banana-bread.md` in Drive, read it, report the bake time.

| Provider path | Model | Result | In MIL? |
| --- | --- | --- | --- |
| Bedrock Converse | `claude-haiku-5-5` | works | yes |
| Bedrock Mantle, OpenAI Responses | `openai.gpt-oss-120b` | works | **no** |
| Bedrock Mantle, OpenAI Chat Completions | `openai.gpt-oss-120b` | works | **no** |
| OpenAI Responses | `gpt-4.1-mini` | works | no |
| OpenAI Chat Completions | `gpt-4.1-mini` | works (chose `download_file_content`) | no |
| Anthropic API via LiteLLM | `anthropic/claude-haiku-5-5` | works | no |
| Gemini | `gemini-3.5-flash` | works (`gemini-2.5-flash` 404 for a new key) | no |

- **Hook payloads were identical across all of them.** Models differ in which tool they pick and in optional arguments (`exportMimeType`, `snippetVerbosity`, `includeComments`, `pageSize`).
- **The Gateway interceptor** saw the same MCP `tools/call` shape for all of these runs (25 calls: 3 structural shapes, request, result and one error; argument sets varied per call). It cannot tell which provider the model used, since the Gateway only receives the tool call.
- Non-Bedrock keys are stored in AgentCore Identity (copying a key into the account's Secrets Manager); the harness role needs read access to them.
- A Runtime agent was tested with Bedrock only.

---

## 6. Identity, concretely ✅

| Caller | What the Gateway interceptor sees as principal |
| --- | --- |
| Direct signed client (IAM user) | `arn:aws:iam::<account>:user/<name>` |
| Harness calling the Gateway | the **harness execution role**; baggage adds `harness.id`, `session.id`; the `--user-id` is not propagated |
| Runtime agent calling the Gateway | the **Runtime execution role**; baggage adds `session.id` only |
| Caller with a Google ID token on a JWT gateway | no `awsPrincipalArn`; the bearer token (with `passRequestHeaders`) gives `iss, sub, email, hd` |
| Harness whose "API key" is a stored JWT | whoever the stored token represents |

Join keys across surfaces: trace id (`traceparent`), session id (baggage), `toolUseId` (hooks only), `REQUEST_ID` (Gateway and tool Lambda). Measured on 28 Gateway calls made by a Harness ✅: the hook's `sessionId` equals the Gateway's baggage `session.id` (28 of 28), the trace id is shared but spans the whole invocation (up to 2 calls in ours), `toolUseId` never reaches the Gateway (it sees JSON-RPC ids 2 and 3 and its own `REQUEST_ID`), and the Gateway's span id never equals a hook's (0 of 28). A call-level join needs trace id, tool, arguments and time order, which matched all 28 calls without ambiguity. The trace id is one per invocation (one `InvokeHarness` or `InvokeAgentRuntime` request) ✅: every Harness hook of an invocation shares it, and so does every Gateway message of that invocation (27 of 27 single-invocation Harness sessions; 22 of 23 Gateway sessions, the other holding two invocations). A session with several invocations has several trace ids, so an inline function's `before_tool_call` (request 1) and `after_tool_call` (request 2) differ in trace id and are paired by `sessionId` and `toolUseId`. The trace id therefore neither spans a session nor separates the calls of one invocation ✅: in a test session of two requests the first request's two tool calls (all their hook and Gateway events) shared one trace id and the second request's call had another. Only `sessionId` spans the session, and only `toolUseId` (Harness side) identifies one call.

### 6.1 What `harnessArn` identifies, and what it does not ✅

In the captured hook events (84 events, 21 sessions) the `harnessArn` never changed, although we ran the same harness with seven different model and provider configurations through per-invocation overrides, and the harness was redeployed up to version 5 (`GetHarness.harnessVersion`).

| It identifies | It does not identify |
| --- | --- |
| The harness resource: the stable slot for "this agent" | The version or endpoint (the qualifier, e.g. `DEFAULT`, is only in the `baggage` header) |
| | The effective model, prompt or tools of that run (overrides replace them) |
| | The caller or end user |
| | The execution role (the AWS identity that holds the permissions); `GetHarness` returns `executionRoleArn`, and the same role is what a Gateway interceptor sees as principal |

So a Harness has two identities that must be joined by a lookup: the **harness** (application, in hooks, and as `harness.id` in Gateway baggage) and its **execution role** (permissions, as the Gateway principal). A Runtime agent's equivalent of the harness ARN is its Runtime ARN, which none of the payloads we captured carry; we saw only its execution role and the session id.

---

## 7. The Google Drive MCP saga ⛔ (and the stand-in)

**Goal:** test the whole path with a real, sensitive-tagged Drive file (`banana-bread.md`) so SlashID's existing Drive connection and tag could be matched by reference.

**What worked** ✅
- A JWT Gateway (Google as issuer) with the Drive MCP server as a 3-legged OAuth target; sign-in, consent, session binding and `tools/list` all succeeded, showing Google's real tool names and schemas (`search_files`, `list_recent_files`, `read_file_content`, `download_file_content`, `get_file_metadata`, ...).
- 3-legged OAuth is rejected on IAM gateways; URL elicitation needs MCP version `2025-11-25` on the Gateway **and** the `MCP-Protocol-Version` header on every request; each caller consents separately from the target's admin sync.
- The interceptor logged the caller's JWT claims.

**What failed** ⛔
- Every real Drive tool call, even `list_recent_files` with no arguments, answers "The caller does not have permission", **directly against Google too**, for two accounts in two domains, with `drive.readonly` alone and with both guide scopes, with a newly created AI-agent client, after the Preview program email said the project was registered, with both APIs enabled and `roles/serviceusage.serviceUsageConsumer` granted. The plain Drive REST API accepted the same token (and returned Drive's own checksums).
- The only informative signal: with an explicit `x-goog-user-project` header the server first said the user lacked Service Usage Consumer on the project (fixed by the grant), after which the generic denial returned.

**Real file facts** ✅ (via `claude-work`, org inference hook in shadow mode):

| | |
| --- | --- |
| id | `<tagged-file-id>` (text/markdown, 1086 bytes, owner `<owner-email>`) |
| md5 / sha1 | `02488f4517260ca221b78f3d5e4f492f` / `e1ef79aac6a3eb336d81298aaf9cdb2ef805a93f` |
| sha256 | `e5b435854c524c1fcb42a81652144141c618d825453b34c4545ffdf173b37368` (equals Drive's own checksum) |
| `download_file_content` | `{content (base64 of the exact bytes), id, mimeType, title}` |
| `read_file_content` | `{fileContent (a markdown-escaped conversion, **not** the bytes), title, viewUrl}` |
| envelope | every result carries both `content` and `structuredContent` |
| with the inference hook enforcing | the request after the search result was denied: "A file in this request is marked sensitive in your organization" |

**Stand-in** ✅: a Lambda target `gdrive` with Google's real tool names and input schemas, Google's output field names, the real file id (via an environment variable) and, with the user's OK, the real bytes. Its download hashes equal the real file's, verified through the Gateway.

**Hash vs reference:** a content hash exists only for a `download_file_content` result. `read_file_content` returns a conversion, so only the file **reference** (`fileId` in the arguments) identifies the file there. Models choose either tool unpredictably.

OneDrive/SharePoint was considered and set aside: Microsoft's hosted server appears admin-managed without bring-your-own app registration, and our `m365` handlers key on the claude.ai and Codex connectors' shapes.

---

## 8. Test log

| # | Test | Outcome |
| --- | --- | --- |
| 1 | Runtime agent (Strands, Bedrock) deployed; traces and OTEL logs read | ✅ prompts and tool args in OTEL logs; model id and tokens in X-Ray spans |
| 2 | Harness with shell tool; four hooks attached | ✅ payloads captured; no identity or model id in them |
| 3 | Gateway with Lambda targets and pass-through interceptor | ✅ MCP payloads, principal, tool-name prefix, client context |
| 4 | Deny via REQUEST interceptor (result style and error style) | ✅ model told the reason in both |
| 5 | Deny via `before_tool_call` | ✅ Gateway never reached |
| 6 | Hooks after `agentcore deploy` | ✅ they are removed |
| 7 | Inference gateway (`bedrock-runtime`): InvokeModel, streaming, Anthropic, OpenAI dialects | ✅ native bodies; buffered stream; no principal |
| 8 | JWT gateway with Google issuer; JWT claims logged by interceptor | ✅ |
| 9 | Drive MCP via Gateway (OAuth) | ✅ plumbing, ⛔ real calls denied by Google |
| 10 | `claude-work` against real Drive | ✅ real shapes and hashes (needed hook in shadow mode) |
| 11 | Drive stand-in with real bytes; Harness and Runtime scenarios | ✅ |
| 12 | Providers via Harness: Mantle, OpenAI, Anthropic, Gemini | ✅ identical hook shapes |
| 13 | Harness model traffic through the Gateway with `apiBase` | ✅ works; static-credential limits |
| 14 | Runtime agent calling the Gateway (MCP client, SigV4) | ✅ role principal, baggage `session.id`, no code of ours |
| 15 | AgentCore Policy: LOG_ONLY then ENFORCE; decision logs | ✅ |

---

## 9. What exists in the account

All `agentdemo-*` in account `<account>`, `us-east-2`; none are production.
- **CDK stack** `AgentCore-AgentDemo-default`: one Runtime agent, one Harness.
- **Gateways:** an IAM gateway (targets: toy tools, Drive stand-in; interceptor; Policy engine in LOG_ONLY; vended logs), an inference gateway (IAM), a JWT gateway (real Drive target, OpenAI inference target).
- **Lambdas:** capture hook, interceptor (with deny switches, both off), tool, Drive stand-in.
- **Identity:** OAuth provider (Google), API-key providers (OpenAI, Anthropic, Gemini).
- **Roles and inline policies** added by hand to the CDK roles (Mantle permission, provider key reads, Gateway invoke).
- **Account-wide side effect:** CloudWatch Transaction Search enabled by the first deploy.
- **State now:** the harness has no hooks attached (cleared by the last deploy).
- The setup and capture scripts are not part of this repository.

---

## 10. Practical gotchas

- The `agentcore` CLI cannot declare hooks or interceptors. Use the `aws` CLI (`--hooks`, `--interceptor-configurations`, `--policy-engine-configuration`; verified with 2.39.3) or a current boto3.
- Identity calls (`GetResourceOauth2Token`, `GetResourceApiKey`, `GetWorkloadAccessToken`) must be allowed on the provider, the token vault **and** the workload-identity resources.
- Gateway tool schemas reject `enum`; `protocolType` accepts only `MCP`; a target in `*_PENDING_AUTH` cannot be updated or deleted for about 10 minutes.
- `claude-work` needs a fresh login when it answers a 403 scope error.
- In this environment the auto-mode classifier blocks scanning for credentials and writing a secret to a store unless the user explicitly authorizes it.

---

## 11. Not known yet

- How the Google Drive MCP gating resolves (user membership of the Preview program group? propagation? another entitlement).
- Whether an interceptor's latency budget is comfortable: Policy adds ~38 ms; interceptor and hook Lambda latency were not measured.
- Behavior when an interceptor Lambda errors or times out (Gateway side); Harness hooks have `failureMode`, the Gateway's behavior is undocumented to us.
- Whether the server's preflight endpoint answers for tool calls from a source with no model.
- Streaming-on interceptor behavior; Policy temporal and guardrail conditions; Gateway metrics.
- A Runtime agent with non-Bedrock models, and a Strands agent with in-process hooks.

---

## 12. Constraints the design has to respect

These are facts from the tests, not decisions.

1. **The unit of an event.** `AIInvocationObservedV1` assumes one inference round (`model`, tokens, a transcript). Tool-call surfaces see one tool call with no model. The shared builder and the preflight endpoint will have to accept that, as the Codex hooks already do.
2. **Two surfaces see the same call** on a Harness with Gateway tools (hook and interceptor). They share a trace id. De-duplication or a rule per agent shape is needed.
3. **Runtime agents have only the Gateway path.** Tools outside a Gateway are invisible to every external surface.
4. **Identity is the weakest dimension.** Report the role, the agent (`harnessArn`/`harness.id`) and the session; a human only via a JWT gateway.
5. **Deny semantics differ:** hook deny = reason as tool result; interceptor deny = a body we choose; Policy deny = `-32002` with its own text. A RESPONSE interceptor can recognise a Policy denial by that code; a hook cannot tell who denied.
6. **Operational fragility differs by surface.** Harness hooks vanish on every deploy and `UpdateHarness` replaces the whole list, so attaching must read-merge-write. Gateway interceptors persisted across deploys (the project does not manage those gateways), but the CLI cannot declare them, a Gateway has one slot per type, and an `UpdateGateway` that omits them clears them (tested), so attaching, and every later gateway update, must resend them. Both are attached per resource, with no org-wide switch.
7. **Statelessness:** the `tools/list` catalog arrives once per session; the HTTP-shape RESPONSE has no request; hook `after_tool_call` needs the earlier verdict. A stateless Lambda must either carry a cache or emit events at the moment it knows enough.
8. **Content vs reference:** hashes come only from `download_file_content` results; `read_file_content` gives only a reference. Preflight on arguments (reference) works before the file is read.
9. **Model-call detail** is optional and depends on traces, MIL or an agent change.
10. **Latency and failure mode** are in the user's critical path; fail-open versus fail-closed must be a deliberate setting on both surfaces.
11. **Keeping a sensitive result from the model works on one surface only.** Pre-call deny works on both (S1 `before_tool_call`, S2 REQUEST). Result-side protection works through the S2 RESPONSE interceptor, for Gateway tools only, and it has to inspect every result (search snippets leak). S1's `after_tool_call` deny stops the run but the result is retained and reaches the model on the next request; Harness tools that bypass a Gateway (`shell`, `remote_mcp`, inline functions) therefore have no result-side control, only the pre-call check.

---

## Appendix A. Example payloads (generalized)

**Hook, `before_tool_call` (Harness calling a Gateway tool)**
```json
{
  "event": "before_tool_call", "name": "capture_before_tool_call",
  "harnessArn": "arn:aws:bedrock-agentcore:us-east-2:<account>:harness/AgentDemo_default_hdemo-<id>",
  "sessionId": "<uuid>",
  "context": {"toolName": "gdrive___read_file_content", "toolType": "agentcore_gateway",
              "toolInput": {"fileId": "<tagged-file-id>"}, "toolUseId": "tooluse_..."},
  "headers": {"traceparent": "00-<trace>-<span>-01",
              "baggage": "Self=...,harness.endpoint.qualifier=DEFAULT,harness.id=<id>,session.id=<uuid>"}
}
```

**Gateway interceptor, MCP shape, REQUEST for a Runtime agent's call**
```json
{
  "interceptorInputVersion": "1.0",
  "mcp": {
    "gatewayRequest": {
      "path": "/mcp", "httpMethod": "POST",
      "headers": {"traceparent": "00-<trace>-<span>-01", "baggage": "Self=...,session.id=<uuid>",
                  "mcp-protocol-version": "2025-03-26", "Authorization": "AWS4-HMAC-SHA256 ..."},
      "body": {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "gdrive___read_file_content", "arguments": {"fileId": "<tagged-file-id>"},
                          "_meta": {"traceparent": "...", "baggage": "..."}}},
      "context": {"identity": {"awsPrincipalArn": "arn:aws:iam::<account>:role/AgentCore-...-ApplicationRuntimeAgentRu-..."}}
    },
    "gatewayResponse": null
  }
}
```
Lambda client context: `{"GATEWAY_ARN": "...", "GATEWAY_ACCOUNT_ID": "<account>", "REQUEST_ID": "<uuid>", "SOURCE_IP": "..."}`.

**Interceptor deny (REQUEST), what we return**
```json
{"interceptorOutputVersion": "1.0",
 "mcp": {"transformedGatewayResponse": {"statusCode": 200,
   "body": {"jsonrpc": "2.0", "id": 2,
            "result": {"isError": true, "content": [{"type": "text", "text": "Denied by policy: ..."}]}}}}}
```

**Hook deny**: `{"decision": "deny", "reason": "Denied by policy: ..."}`

**HTTP-shape interceptor, RESPONSE (inference proxy)**
```json
{"interceptorInputVersion": "1.0",
 "http": {"gatewayRequest": null,
          "gatewayResponse": {"statusCode": 200, "contentType": "text/event-stream; charset=utf-8",
                              "body": "<base64 of the whole SSE stream>"}}}
```

**Policy decision record (Gateway application log, `LOG_ONLY`)**
```json
{"log": "Policy evaluation denied request", "id": "7",
 "policy": {"decision": "DENY", "reason": "Policy evaluation denied due to forbid_tagged_read-<id>",
            "determiningPolicies": ["forbid_tagged_read-<id>"],
            "principal": {"entityType": "AgentCore::IamEntity", "entityId": "arn:aws:iam::<account>:user/<name>"},
            "latencyMs": 36, "temporal_evaluation_invoked": false}}
```

**Policy denial seen by the caller (`ENFORCE`)**
```json
{"jsonrpc": "2.0", "id": 7,
 "error": {"code": -32002,
           "message": "Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to forbid_tagged_read-<id>]"}}
```

---

## Appendix B. Where things are

- Setup and capture scripts, and the demo agent and harness, are not part of this repository.
- The design built on this evidence: `docs/superpowers/specs/2026-10-30-agentcore-tool-call-forwarder-design.md`.
- Raw captures live in the session scratchpad and may disappear; sanitize before turning any into test fixtures.
