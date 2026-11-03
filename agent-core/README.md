# SlashID AgentCore Forwarder

AWS Lambda that checks every tool call of an Amazon Bedrock AgentCore agent with SlashID before the tool runs, and its result before the model sees it. It is called by AgentCore Harness lifecycle hooks (`before_tool_call`, `after_tool_call`) and by Gateway interceptors (REQUEST and RESPONSE on `tools/call`), and answers allow or deny. It is preflight only: nothing is pushed, and the check sends tool names, ids, identity and file references, never prompts, tool arguments or results.

```
Harness hook  ──┐
                ├─→ Lambda (this repo) ──→ POST /ip/nhi/events/ai-invocations/preflight ──→ SlashID
Gateway interceptor ┘          │
                               └──→ allow, or deny with a reason the model receives
```

A Runtime agent is covered only for tools behind a Gateway. `before_invocation`, `after_invocation`, `initialize` and `tools/list` pass through. Design: [`docs/superpowers/specs/2026-10-30-agentcore-tool-call-forwarder-design.md`](../docs/superpowers/specs/2026-10-30-agentcore-tool-call-forwarder-design.md); what AgentCore exposes: [`docs/agentcore/agentcore-findings.md`](../docs/agentcore/agentcore-findings.md).

## Install

Tagged releases (`agentcore-vX.Y.Z`) publish `slashid-ai-forwarder-agentcore-vX.Y.Z.zip` and `cloudformation.yaml` to GitHub Releases. Upload the zip to an S3 bucket of yours, then deploy the template **in the region of your Harnesses and Gateways** (role lookups use the Lambda's own region).

| Parameter                     | Description                                                                                                                                  |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `LambdaPackageS3Bucket`, `LambdaPackageS3Key` | Where the zip is                                                                                                            |
| `SlashIDPushToken`            | Event-streaming bearer token for the connection (NoEcho)                                                                                      |
| `SlashIDEndpoint`             | (optional, default `https://api.slashid.com`)                                                                                                |
| `FailMode`                    | (default `allow`) Answer when SlashID gives no answer                                                                                        |
| `ShadowMode`                  | (default `false`) Evaluate and log, answer allow                                                                                             |
| `HarnessHooksEnabled`, `GatewayHooksEnabled` | (default `true`) Turn a surface off without detaching it                                                                      |
| `BudgetMs`                    | (default `3500`, 100-6500) Time one check may take                                                                                           |
| `GatewayRoleArns`             | Execution role ARNs of the Gateways that use the function; each gets `lambda:InvokeFunction`. Empty for none                                  |
| `AllowAgentCoreServiceInvoke` | (default `true`) Lets `bedrock-agentcore.amazonaws.com` of this account invoke the function. Unconfirmed that Harness hooks need it or that this is the principal's name; set `false` for Gateway-only use |
| `LogLevel`, `LogRetentionDays` | (default `INFO`, 30) Keep `INFO` or lower: the lines to alert on are INFO                                                                   |

The function (Python 3.14, 512 MB, 10 s) has the basic execution role plus `bedrock-agentcore:GetHarness` on `harness/*` and `bedrock-agentcore:GetGateway` on `gateway/*` of the account and region. Only the listed Gateway roles and the AgentCore service principal can invoke it: anyone who can invoke it can supply the identity it reports.

The deployment prints the function ARN as `ForwarderFunctionArn`; below it is `$FN`.

## Attach to a Harness

`UpdateHarness` replaces the whole hook list, so read, merge ours in, write. Hooks are not part of the `agentcore` project file, and **every `agentcore deploy` removes them: repeat this after each deploy.** Set `MODE` to the stack's `FailMode`, and to `allow` when `ShadowMode` is on (AWS applies `failureMode` to a Lambda error whatever the shadow setting says).

```bash
HARNESS_ID=...          # harness id, not the ARN
FN=arn:aws:lambda:<region>:<account>:function:<stack>-forwarder
MODE=allow              # = FailMode; allow when ShadowMode is true

aws bedrock-agentcore-control get-harness --harness-id "$HARNESS_ID" \
  --query 'harness.hooks' --output json > current-hooks.json

jq --arg fn "$FN" --arg mode "$MODE" '
  def target: {lambda: {arn: $fn, timeoutSeconds: 10, failureMode: $mode}};
  (. // [])
  | map(select((to_entries[0].value.name // "") | startswith("slashid_") | not))
  + [{beforeToolCall: {name: "slashid_before_tool_call", target: target}},
     {afterToolCall:  {name: "slashid_after_tool_call",  target: target}}]
' current-hooks.json > hooks.json

aws bedrock-agentcore-control update-harness --harness-id "$HARNESS_ID" --hooks file://hooks.json
```

Changing `FailMode` or `ShadowMode` on the stack means running this again so `failureMode` follows.

## Attach to a Gateway

A Gateway takes one REQUEST and one RESPONSE interceptor. The function is attached to both, with `passRequestHeaders` on:

```bash
cat > interceptors.json <<EOF
[{"interceptor": {"lambda": {"arn": "$FN"}},
  "interceptionPoints": ["REQUEST", "RESPONSE"],
  "inputConfiguration": {"passRequestHeaders": true}}]
EOF

aws bedrock-agentcore-control update-gateway --gateway-identifier "$GATEWAY_ID" \
  --name "$NAME" --role-arn "$GATEWAY_ROLE_ARN" --protocol-type MCP --authorizer-type AWS_IAM \
  --interceptor-configurations file://interceptors.json
```

`passRequestHeaders` is how the function reads the `User-Agent` and `baggage` headers (the Harness id and session id of a Harness calling the Gateway). **It also puts the caller's `Authorization` header (a JWT or a SigV4 signature) in the Lambda payload.** The function never reads or logs it, but it is in the event AWS hands over. The Gateway's role (`GatewayRoleArns`) must be in the stack.

An `UpdateGateway` that omits `interceptorConfigurations` clears them (tested), so every later update of the Gateway, by anything, must resend them; send the other current settings (`authorizerConfiguration`, `exceptionLevel`, ...) from `get-gateway` too. A customer's own interceptor and ours conflict: there is one slot per type, and chaining is not supported. For a JWT-authorized Gateway use `--authorizer-type CUSTOM_JWT` and its `--authorizer-configuration`; the token is never read, so it is checked like any other.

## Settings

All are environment variables set by the template.

| Variable                         | Default | Meaning                                                                                                   |
| -------------------------------- | ------- | --------------------------------------------------------------------------------------------------------- |
| `SLASHID_ENDPOINT`               | `https://api.slashid.com` | SlashID API endpoint                                                                    |
| `SLASHID_PUSH_TOKEN`             | required | Bearer token; also the key of the deny reference. A missing token is a Lambda error                     |
| `SLASHID_HARNESS_HOOKS_ENABLED`  | `true`  | When false, Harness payloads are answered allow without calling SlashID                                    |
| `SLASHID_GATEWAY_HOOKS_ENABLED`  | `true`  | When false, Gateway payloads are echoed without calling SlashID                                           |
| `SLASHID_VERDICT_FAIL_MODE`      | `allow` | `allow` or `deny`: the answer when a check gets none                                                       |
| `SLASHID_VERDICT_BUDGET_MS`      | `3500`  | Bound on one check (100-6500); role lookups before it take up to 2 s, so 2 s + budget + overhead stays under the 10 s hook and Lambda timeouts |
| `SLASHID_SHADOW_MODE`            | `false` | Evaluate and log, answer allow                                                                            |
| `LOG_LEVEL`                      | `INFO`  | Python log level                                                                                          |

`SLASHID_INCLUDE_RAW_CONTENT` is forced off: connector files carry no text.

### Fail mode

A check that gets no answer (SlashID unreachable, a non-200, an unparseable body, the budget spent, or a Gateway call with neither a Gateway ARN nor a principal to send) takes `SLASHID_VERDICT_FAIL_MODE`, on every check. A 200 is always a verdict. A fail-mode deny reads "Your organization's policy check is unavailable." A payload we cannot read (a malformed `tools/call`, one nested too deep, a JSON-RPC batch containing a `tools/call`) is denied whatever the fail mode, since that setting is for SlashID being down, not for a caller choosing what we can read. Shadow mode answers allow in place of any deny, those included, and logs a `shadow` line with both.

When the Lambda itself errors or times out, **AWS decides, not this setting**: on a Harness the hook's `failureMode` (set by the recipe above); on a Gateway the behavior is unconfirmed, so "fail mode deny" is not promised for a Lambda error there.

## What to alert on

Logs are one JSON object per line with a fixed set of keys, never a payload, header, baggage value, tool argument or result. With the `allow` default an outage is otherwise silent. Alert on:

| `event`                    | Meaning                                                                                                              |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `fail_mode`                | A check got no answer, or a payload was unreadable; `surface`, `check`, `reason`, `mode` say which and what was applied |
| `pass_through`             | A payload went through unchecked: `unknown_payload`, `surface_disabled` (once per container start), `streaming_response` |
| `baggage_unverified`       | A Gateway call claimed a Harness (baggage `harness.id`) that could not be verified; `agent_arn` is left out           |
| `role_lookup_failed`       | `GetHarness` or `GetGateway` failed (`access_denied`, `not_found`, `throttled`, `timeout`, ...); WARNING             |
| `gateway_role_unavailable` | A Gateway call without a principal got no role from `GetGateway`                                                     |

Others: `shadow`, `truncated_input`, `cache_miss`, `unreadable`, `missing_request_id`, `unexpected_error` (ERROR). A format change on AWS's side shows up as `fail_mode` with reason `unknown_payload`, `unknown_version` or `unknown_event`; with the `allow` default only that line says result checks stopped.

## Deny references

Every deny text ends with ` (ref <12 hex>)`: a short HMAC of the text before it, keyed from the push token, so every container of one connection agrees with no shared state. AgentCore fires the result-side hook after a denied call too (`after_tool_call` after a denied `before_tool_call`, RESPONSE after a REQUEST deny) with the deny text as the result, and nothing marks who denied. A result that is exactly one text block, no `structuredContent`, whose reference verifies, is recognized as ours and answered allow without calling SlashID, on either surface. The reference is visible to the model and the user; repeating it is harmless and works as a support reference. Rotating the push token makes older denies stop verifying, and they are then checked like any result. A deny reason is capped at 500 characters (the Anthropic hook's bound); several reasons are joined with "; ".

How a deny lands: Harness `before_tool_call` skips the call and the model receives the reason as the tool result. Harness `after_tool_call` stops the run (`hook_stopped`), but **the result stays in the session and reaches the model on the next request**: it stops the agent without withholding the result. Gateway REQUEST skips the tool with an `isError` result carrying the reason; Gateway RESPONSE replaces the result with `Content withheld: <reasons>`.

## Known limitations

- **64 KiB.** A Harness hook's context is capped at 64 KiB: a larger tool result arrives replaced by `{truncated, preview}` (captured), so its status is lost (it is sent as not an error) and its file references come only from the cached call input. Whether a large tool input is truncated the same way is unconfirmed; if it is, a call padded past the limit loses its file references, and a `truncated_input` log line says so. The Gateway payload limit is 6 MB; what the Gateway does beyond it is unconfirmed.
- **Region.** Role lookups go to `bedrock-agentcore-control` in the Lambda's own region, not the ARN's. Deploy the function in the region of its Harnesses and Gateways; a Harness in another account or region fails its lookup.
- **Missing permission.** Until the SlashID server accepts `agent_arn` and `gateway_arn` and a missing `principal_arn`, an event with no role has no identifier the server recognizes. A missing `GetHarness` or `GetGateway` permission (or a failed lookup) is therefore a fail-mode outcome, logged as `role_lookup_failed` and `fail_mode`.
- **`agent_arn` on the Gateway path is weak.** It is rebuilt from the caller-set baggage `harness.id` and set only when `GetHarness` for that Harness returns the caller's own role. That shows the claimed Harness shares the role, not that the caller is that Harness: another Harness or Runtime agent with the same role can claim it. Treat it as no stronger than the role. `conversation_id` (baggage `session.id`) is a claim too.
- **Error results.** An `isError` result (or an `error` reply) is still checked, with `is_error` set and no file references.
- **Two surfaces, one call.** A Harness with Gateway tools fires both surfaces for one call: up to four serial checks, each bounded by the budget (14 s at the default, plus up to 1 s per cold role lookup, with a slow but reachable server) and no circuit breaker. Both are checked; either deny blocks. Turn one surface off if that is not worth it.
- **Input cache.** A Harness `after_tool_call` carries no tool input, so the input of an allowed `before_tool_call` that had file references is kept in the container's memory (10 minutes, 1000 entries, never sent or logged). A result handled by another or a recycled container is checked from the result alone, logged as `cache_miss`.
- **Streaming and batches.** A RESPONSE flagged `isStreamingResponse` is echoed unchecked (`pass_through`; untested). A JSON-RPC batch containing a `tools/call` is denied (checking each element is deferred); one without is echoed.
- **File references** are extracted only for recognized connector tools (Drive, Microsoft 365, SharePoint names, on any server name). Only the Drive shapes were seen on AgentCore, through a stand-in.

### Unconfirmed

Not yet verified against AWS or the SlashID server (design spec, section 6):

- that the server accepts a tool-only preflight with an unknown model, no `event_type`, and `agent_arn` or `gateway_arn` without `principal_arn` (this gates v1);
- whether Harness hooks need a resource policy on the function and the service principal's name;
- what the Gateway does when the interceptor Lambda errors, times out, receives a result over 6 MB or a streaming response;
- that the baggage `harness.id` rebuilds the `harnessArn`, and that `REQUEST_ID` is the same on a call's REQUEST and RESPONSE;
- that the deny reference survives each hop unchanged;
- the shape of an allow echo without response headers, of a JSON-RPC batch deny, and of an `isError` replacement on a RESPONSE.

## Development

Requirements: `uv`, `pre-commit`.

```bash
uv sync                       # install workspace dependencies
uv run pytest                 # run tests
uv run ty check               # type-check
uv run ruff check             # lint
uv run ruff format            # format
```

## Live checks

`tests/live` sends the fixtures through the handlers to a real SlashID server: a call check with a connector ref, a result check, a gateway-only identity, an `agent_arn` event and the deny round trip. They are collected but skipped unless `SLASHID_LIVE=1`.

```bash
SLASHID_LIVE=1 SLASHID_PUSH_TOKEN_FILE=path/to/token uv run pytest tests/live -s
```

The token is read from that file and never printed; `SLASHID_ENDPOINT` overrides the endpoint. Use the test tenant: every id is synthetic (`live-test-...`, `123456789012`, `EXAMPLE`), but the server records each preflight it receives. With `-s` each test prints the HTTP status, the outcome, the number of deny reasons (not their text) and the latency.

## Releases

`agentcore-vX.Y.Z` tags (matching `version` in `pyproject.toml`) publish:

- `slashid-ai-forwarder-agentcore-vX.Y.Z.zip` — Lambda deployment package
- `cloudformation.yaml` — install template
