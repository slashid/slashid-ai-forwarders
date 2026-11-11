# SlashID forwarder for Portkey

Puts SlashID's AI policy in the path of every model call that goes through a
[Portkey](https://portkey.ai) gateway, whichever provider serves it. Portkey
calls this service as a webhook guardrail before the request leaves and after
the model answers; the service asks SlashID and returns the verdict.

| Hook               | What SlashID judges                                                                                                          | What happens on a deny                                                |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Before the request | The caller, the model, the fresh round of the conversation (the prompt, tool results and inline files since the last answer) | Portkey does not call the provider and returns 446 to the application |
| After the response | The tool calls the model asked for, with their arguments, before the application runs any                                    | Portkey returns 446 in place of the answer                            |

After the response the service also records the invocation (model, tokens,
tools, files) in SlashID.

## Setup

1. In SlashID, create the connection the gateway reports to and copy its
   Event streaming token.
2. Run the image (`ghcr.io/slashid/slashid-ai-forwarder-portkey:<version>`;
   `docker run -p 8080:8080 <image>`, or build it with
   `docker build -f portkey/Dockerfile .` from the repository root)
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

| Variable                      | Default                   | Meaning                                                                                                                                 |
| ----------------------------- | ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `SLASHID_ENDPOINT`            | `https://api.slashid.com` | The SlashID API                                                                                                                         |
| `SLASHID_VERDICT_FAIL_MODE`   | `allow`                   | The answer for a request that could not be judged: `allow` or `deny`                                                                    |
| `SLASHID_VERDICT_BUDGET_MS`   | `2500`                    | How long to wait for SlashID. Keep it under the guardrail's `timeout`                                                                   |
| `SLASHID_SHADOW_MODE`         | `false`                   | Judge and log, then answer allow                                                                                                        |
| `SLASHID_PREFLIGHT_CONTENT`   | `true`                    | Send the fresh round's text with the check. SlashID evaluates it and does not store it; rules on prompt content cannot match without it |
| `SLASHID_INCLUDE_RAW_CONTENT` | `false`                   | Keep the text on the recorded invocation too                                                                                            |
| `SLASHID_PUSH_EVENTS`         | `true`                    | Record each answered request                                                                                                            |
| `SLASHID_USER_METADATA_KEY`   | `_user`                   | The metadata key naming the person                                                                                                      |
| `SLASHID_AGENT_METADATA_KEY`  | `agent`                   | The metadata key naming the agent                                                                                                       |
| `SLASHID_MAX_BODY_BYTES`      | `33554432`                | Largest webhook body accepted                                                                                                           |

## What is not judged

A request the service cannot judge takes `SLASHID_VERDICT_FAIL_MODE`. That
covers:

- A request whose metadata names neither a user nor an agent.
- Request types other than chat completions: `/v1/messages`, legacy
  completions and embeddings.
- A check SlashID did not answer within the budget.

Two limits come from the gateway:

- Portkey hands a webhook no response body for a streamed answer, so the
  tool calls of a streamed answer are not judged and its tokens are not
  recorded. The before-request check is unaffected.
- A webhook call that fails outright (a timeout, a non-200) is Portkey's to
  decide: the guardrail's own fail-on-error setting applies.

Text longer than `SLASHID_MAX_CONTENT_SIZE` (100,000 characters) is cut in
the middle before it is sent; a rule on prompt content does not see the cut
part.

## Live check

1. Without the gateway, post a before-request hook straight to the service. The token is the connection's Event streaming token, and `_user` must be a person SlashID knows by that address:

   ```bash
   curl -s -X POST http://localhost:8080/hooks/portkey \
     -H "Authorization: Bearer $SLASHID_PUSH_TOKEN" -H 'content-type: application/json' \
     -d '{"eventType": "beforeRequestHook", "provider": "openai", "requestType": "chatComplete",
          "metadata": {"_user": "<email>"},
          "request": {"json": {"model": "gpt-5", "messages": [{"role": "user", "content": "hello"}]},
                      "text": "hello", "isStreamingRequest": false, "isTransformed": false},
          "response": {"json": {}, "text": "", "statusCode": null, "isTransformed": false}}'
   ```

   An allowed request answers `{"verdict":true}`; a denied one answers `{"verdict":false,"data":{"deny_reasons":[...],"explanation":"..."}}`. The service logs `verdict <id>: allow, answered allow`.

2. Through the gateway, with the guardrails from Setup on the config: send a chat completion with `metadata` naming the user, then one that mentions a file you tagged as sensitive. The first returns the model's answer and its invocation appears in SlashID; the second returns 446.

## Development

```bash
uv sync
cd portkey
uv run pytest
uv run ruff check .
uv run ty check
```
