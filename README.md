# SlashID Bedrock Forwarder

CloudWatch-triggered AWS Lambda forwarding AWS Bedrock Model Invocation events to SlashID — strips bodies, forwards only model and tool metadata.

## Architecture

The Lambda runs in the customer's AWS account. CloudWatch Logs subscription delivers each Bedrock invocation, the handler extracts identity, model, token, and tool metadata (no prompt/response text and no tool arguments leave the account), and POSTs to SlashID's NHI AI invocations endpoint.

```
Bedrock call ──→ MIL ──→ CloudWatch Logs ──┐
                                            ▼
                                   Lambda (this repo)
                                            │
                                            ▼
                              POST /nhi/events/ai-invocations
                                            │
                                            ▼
                                  SlashID NHI subgraph
```

On terminal failure, the Lambda's async-invoke DLQ (SQS) catches the event for later replay.

## Install

> Coming in v0.1 — published as a CloudFormation template attached to each GitHub release.

The customer provides:

| Parameter | Description |
|---|---|
| `BedrockLogGroupName` | The CloudWatch log group MIL writes to |
| `SlashIDEndpoint` | e.g. `https://api.slashid.com` |
| `SlashIDOrgId` | Organization UUID |
| `SlashIDConnectionId` | UUID of the SlashID push connection that receives events |
| `SlashIDPushToken` | Event-streaming bearer token for that connection (NoEcho) |

The push token is the only credential the Lambda needs. Identity creation and STS role-chain unrolling happen on the SlashID side.

## Development

Requirements: `uv`, `pre-commit`.

```bash
uv sync                       # install dependencies into .venv
uv run pre-commit install     # set up hooks
uv run pytest                 # run tests
uv run ty check               # type-check
uv run ruff check             # lint
uv run ruff format            # format
```

## Releases

Tagged releases publish two artifacts to GitHub Releases:

- `slashid-bedrock-forwarder-<version>.zip` — Lambda deployment package
- `cloudformation.yaml` — install template
