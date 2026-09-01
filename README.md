# slashid-ai-forwarders

Monorepo of SlashID forwarders that observe AI-provider invocations and forward metadata (identity, model, tokens, tool use) to SlashID's NHI subgraph.

## Components

- [`bedrock/`](bedrock/README.md) — AWS Bedrock forwarder Lambda. CloudWatch Logs subscription → SlashID.
- `shared/` — internal library reused across forwarders (event schema, HTTP sink, content hashing, base config, Anthropic normalizer).

Future forwarders (Vertex AI, and others) will join as sibling subdirectories.

## Development

Requirements: `uv`, `pre-commit`.

```bash
uv sync                                    # install workspace deps + dev tools
uv run pre-commit install                  # set up hooks

# per-subproject (tool config lives in each subproject's pyproject.toml)
(cd bedrock && uv run pytest)              # Bedrock tests
(cd shared  && uv run pytest)              # Shared library tests
(cd bedrock && uv run ruff check .)        # Bedrock lint
(cd shared  && uv run ruff check .)        # Shared lint
(cd bedrock && uv run ty check)            # Bedrock type-check
(cd shared  && uv run ty check)            # Shared type-check
```

## Releases

Tagged releases are per-component with a component prefix:

- `bedrock-vX.Y.Z` → publishes Lambda zip + CloudFormation template to GitHub Releases (see `bedrock/README.md`).

The version in the tag must match the `version` field in the component's `pyproject.toml`. The release workflow enforces this.
