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

On terminal failure, the Lambda exhausts AWS's two built-in async-invoke retries and CloudWatch's `Errors` metric increments — set an alarm on it. (No SQS DLQ is wired up in v1; add `DeadLetterConfig` to `forwarder.yaml` if you want replay-capable durability.)

## Install

> Coming in v0.1 — published as a CloudFormation template attached to each GitHub release.

The customer provides:

| Parameter                    | Description                                                                                                                                                                                                     |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BedrockLogGroupName`        | The CloudWatch log group MIL writes to                                                                                                                                                                          |
| `BedrockBodyOffloadS3Bucket` | (optional) Bucket Bedrock writes offloaded prompts to. Granted `s3:GetObject` so the Lambda can inline large prompts. Leave blank to skip — offloaded records still ingest, just without tool-catalog metadata. |
| `SlashIDEndpoint`            | e.g. `https://api.slashid.com`                                                                                                                                                                                  |
| `SlashIDPushToken`           | Event-streaming bearer token for the connection (NoEcho)                                                                                                                                                        |
| `IncludeRawContent`          | (optional, default `false`) Opt-in to forwarding raw prompt/response JSON. When off, only content hash + mime type + byte length are sent.                                                                      |

The push token is the only credential the Lambda needs. SlashID derives the org and connection IDs from the token; identity creation and STS role-chain unrolling happen on the SlashID side.

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

## Smoke-testing against a live account

Once the CloudFormation template is deployed against a test AWS account, trigger real Bedrock invocations and confirm `AIInvocationObservedV1` events land on the SlashID side. Two paths worth exercising — they hit different branches of `mil_normalize.normalize_record`. Both use your existing AWS SDK credential chain (env, `~/.aws/credentials`, or `aws sso login`).

### Via AWS Converse (already-Converse pass-through path)

`./converse` invokes a Bedrock native-Converse model — Nova Pro by default. MIL emits these records in Converse shape natively, so the normalizer's Anthropic branch is skipped entirely; this exercises the pass-through and validates that `build_event` handles Converse-shape input directly.

```bash
./converse                            # default prompt, Nova Pro
./converse "your prompt here"         # custom prompt
MODEL_ID=us.amazon.nova-lite-v1:0 ./converse   # different model
```

Requires the AWS CLI and `jq`. For a GUI alternative, the [Bedrock Playground](https://us-east-2.console.aws.amazon.com/bedrock/home?region=us-east-2#/playground?modelId=amazon.nova-pro-v1%3A0) works too — pick any model and type a prompt.

### Via Claude Code (Anthropic → Converse normalizer path)

`./claude` runs Claude Code routed through Bedrock. Every call goes through the Anthropic Messages API, so this exercises the Anthropic-shape branch of the normalizer (both single-shot and streaming, tool_use, thinking blocks).

```bash
./claude                     # interactive session
./claude "explore this repo" # one-shot
```

### Via Converse with a local attachment (`_accessed_files` inline-bytes path)

`./converse-attach <file> [prompt]` base64-encodes a local file, sends it as a Converse `document` (pdf/csv/txt/md/html/doc/docx/xls/xlsx) or `image` (png/jpeg/gif/webp) block, and validates that the emitted `AIInvocationObservedV1.accessed_files` entry carries the file name, IANA media type, byte length, and stable `sha256/sha1/md5` matching the raw bytes.

```bash
./converse-attach ./notes.pdf                        # default prompt
./converse-attach ./chart.png "what is in this image?"
```

### Via Converse with an S3-hosted attachment (`_accessed_files` S3-source path)

`./converse-attach-s3 <s3-uri> [prompt]` sends a document or image whose `source` is an S3 URI rather than inline bytes — exercises `shared/s3.py::_resolve_s3_attachment`'s HeadObject + optional GetObject path in the Lambda. The deployed Lambda must have `s3:GetObject` on the bucket (via the `BedrockBodyOffloadS3Bucket` CFN parameter or an equivalent grant), and the object must exist and be readable by the Bedrock service.

```bash
./converse-attach-s3 s3://my-bucket/report.pdf
./converse-attach-s3 s3://my-bucket/logo.png "describe this logo"
```

### Via Claude Code exercising the `Read` tool (`_accessed_files` tool-result path)

Claude Code's `Read` tool result is correlated back to the file it opened; the forwarder hashes the returned bytes (after `strip_cat_n` strips Claude Code's `n\thello` line-number prefix) and emits an `AIAccessedFile` with the tool's `file_path` argument as the name. Any prompt that reliably fires the `Read` tool works — e.g.:

```bash
./claude "please Read /etc/hostname and repeat the contents verbatim"
```

The same code path also covers OpenCode / Amazon Q Developer / Gemini CLI (`ReadFile`, `read_file`, `view_file`) and the Claude computer-use text-editor tool (`str_replace_based_edit_tool`) — different tool names, same `_READ_TOOLS` table in `shared/src/slashid_ai_forwarder_core/events.py`.

## Releases

Tagged releases publish two artifacts to GitHub Releases:

- `slashid-bedrock-forwarder-<version>.zip` — Lambda deployment package
- `cloudformation.yaml` — install template
