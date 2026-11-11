# SlashID forwarder for Portkey

Puts SlashID's AI policy in the path of every model call that goes through a
[Portkey](https://portkey.ai) gateway, whichever provider serves it. Portkey
calls this service as a webhook guardrail before the request leaves and after
the model answers; the service asks SlashID and returns the verdict.

| Hook               | What SlashID judges                                                                                              | What happens on a deny                                                |
| ------------------ | ---------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Before the request | The caller, the model, and the fresh round of the conversation: the prompt, what tools returned, inline files    | Portkey does not call the provider and returns 446 to the application |
| After the response | The answer's text, and the tool calls the model asked for, with their arguments, before the application runs any | Portkey returns 446 in place of the answer                            |

The prompt, what each tool returned and the answer are sent apart, so a
SlashID rule on one reads that one alone: a rule on tool results blocks a
record a tool fetched without blocking the same words typed in a prompt.

After the response the service also records the invocation (model, tokens,
tools, files) in SlashID.

## Setup

1. In SlashID, create the connection the gateway reports to and copy its
   Event streaming token.
2. Run the image (`ghcr.io/slashid/slashid-ai-forwarder-portkey:<version>`)
   where the gateway can reach it. A self-hosted gateway must list the host
   in `TRUSTED_CUSTOM_HOSTS` when it is on a private network.
3. In Portkey, add a webhook guardrail to the config your requests use, once
   as a before-request hook and once as an after-request hook, both set to
   deny:

   ```json
   {
     "before_request_hooks": [
       {
         "type": "guardrail",
         "id": "slashid-before",
         "deny": true,
         "checks": [
           {
             "id": "default.webhook",
             "parameters": {
               "webhookURL": "https://<forwarder>/hooks/portkey",
               "headers": { "Authorization": "Bearer <Event streaming token>" },
               "timeout": 3000
             }
           }
         ]
       }
     ],
     "after_request_hooks": [
       {
         "type": "guardrail",
         "id": "slashid-after",
         "deny": true,
         "checks": [
           {
             "id": "default.webhook",
             "parameters": {
               "webhookURL": "https://<forwarder>/hooks/portkey",
               "headers": { "Authorization": "Bearer <Event streaming token>" },
               "timeout": 3000
             }
           }
         ]
       }
     ]
   }
   ```

4. Have the application name who each request is for in Portkey's
   `metadata`: the person under `_user` (an email address resolves to the
   person SlashID knows by that address) and, when the call runs for an
   agent, the agent under `agent`.

The token travels with each webhook call and goes on to SlashID unchanged.
The service holds no secret, and one deployment serves any number of
connections.

## Configuration

| Variable                      | Default                   | Meaning                                                                                                                                                              |
| ----------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SLASHID_ENDPOINT`            | `https://api.slashid.com` | The SlashID API                                                                                                                                                      |
| `SLASHID_VERDICT_FAIL_MODE`   | `allow`                   | The answer for a request that could not be judged: `allow` or `deny`                                                                                                 |
| `SLASHID_VERDICT_BUDGET_MS`   | `2500`                    | How long to wait for SlashID. Keep it under the guardrail's `timeout`                                                                                                |
| `SLASHID_SHADOW_MODE`         | `false`                   | Judge and log, then answer allow. SlashID records a deny as noted, not as blocked                                                                                    |
| `SLASHID_PREFLIGHT_CONTENT`   | `true`                    | Send the prompt, what tools returned and the answer with the check. SlashID evaluates them and does not store them; a rule on content blocks whatever it cannot read |
| `SLASHID_INCLUDE_RAW_CONTENT` | `false`                   | Keep the text on the recorded invocation too                                                                                                                         |
| `SLASHID_PUSH_EVENTS`         | `true`                    | Record each answered request                                                                                                                                         |
| `SLASHID_USER_METADATA_KEY`   | `_user`                   | The metadata key naming the person                                                                                                                                   |
| `SLASHID_AGENT_METADATA_KEY`  | `agent`                   | The metadata key naming the agent                                                                                                                                    |
| `SLASHID_MAX_BODY_BYTES`      | `33554432`                | Largest webhook body accepted                                                                                                                                        |

## What is not judged

A request the service cannot judge takes `SLASHID_VERDICT_FAIL_MODE`. That
covers:

- A request whose metadata names neither a user nor an agent.
- Request types other than chat completions: `/v1/messages`, legacy
  completions and embeddings.
- A check SlashID did not answer within the budget.

Two limits come from the gateway:

- Portkey hands a webhook no response body for a streamed answer, so the
  text and the tool calls of a streamed answer are not judged and its
  tokens are not recorded. The before-request check is unaffected.
- A webhook call that fails outright (a timeout, a non-200) is Portkey's to
  decide: the guardrail's own fail-on-error setting applies.

A prompt, a tool result or an answer longer than `SLASHID_MAX_CONTENT_SIZE`
(100,000 characters) is cut in the middle before it is sent; a rule on
content does not see the cut part.

## Development

```bash
uv sync
cd portkey
uv run pytest
uv run ruff check .
uv run ty check
```
