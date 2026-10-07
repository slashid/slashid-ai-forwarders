# slashid-ai-forwarder-codex

SlashID's hook for [OpenAI Codex](https://openai.com/codex/), the CLI and the ChatGPT desktop app.

It runs on a developer's machine as a Codex hook. Before each prompt and tool call it asks SlashID for a verdict, so a prompt or call that reads a file you have tagged sensitive, or that violates one of your policies, can be blocked. After each response it sends SlashID an invocation event: the model, token usage, the tools requested and used, and the files read.

Install it with `uv tool install slashid-ai-forwarder-codex` or `pip install slashid-ai-forwarder-codex`. Requires Python 3.12 or later.

Setup, with or without MDM, configuration and troubleshooting are in the documentation:
**https://console.slashid.com/docs/identity-protection/onboarding/openai/**

Source: [slashid/slashid-ai-forwarders](https://github.com/slashid/slashid-ai-forwarders/tree/main/codex). Apache-2.0.
