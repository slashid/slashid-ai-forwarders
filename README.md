# slashid-ai-forwarders

Monorepo of SlashID forwarders that observe AI-provider invocations and forward metadata (identity, model, tokens, tool use) to SlashID's NHI subgraph.

## Components

- [`bedrock/`](bedrock/README.md) — AWS Bedrock forwarder Lambda. CloudWatch Logs subscription → SlashID.
- [`vertex/`](vertex/README.md) — GCP Vertex AI forwarder Cloud Run service. BigQuery request-response logs → SlashID.
- [`anthropic/`](anthropic/README.md) — Claude Enterprise forwarder on Cloud Run. Inference hooks (inline, with allow/deny) + Compliance API polling → SlashID.
- `shared/` — internal library reused across forwarders (event schema, HTTP sink, content hashing, base config, Anthropic + Converse + Gemini normalizers).

## Development

Requirements: `uv`, `pre-commit`.

```bash
uv sync                                    # install workspace deps + dev tools
uv run pre-commit install                  # set up hooks

# per-subproject (tool config lives in each subproject's pyproject.toml)
(cd bedrock   && uv run pytest)            # Bedrock tests
(cd vertex    && uv run pytest)            # Vertex tests
(cd anthropic && uv run pytest)            # Anthropic tests
(cd shared    && uv run pytest)            # Shared library tests
(cd bedrock   && uv run ruff check .)      # Bedrock lint
(cd vertex    && uv run ruff check .)      # Vertex lint
(cd anthropic && uv run ruff check .)      # Anthropic lint
(cd shared    && uv run ruff check .)      # Shared lint
(cd bedrock   && uv run ty check)          # Bedrock type-check
(cd vertex    && uv run ty check)          # Vertex type-check
(cd anthropic && uv run ty check)          # Anthropic type-check
(cd shared    && uv run ty check)          # Shared type-check
```

## Releases

Tagged releases are per-component with a component prefix:

- `bedrock-vX.Y.Z` → publishes Lambda zip + CloudFormation template to GitHub Releases (see `bedrock/README.md`).
- `vertex-vX.Y.Z` → publishes the service container image to GHCR; the Terraform module is consumed from the tag (see `vertex/README.md`).
- `anthropic-vX.Y.Z` → publishes the receiver container image to GHCR; the Terraform module is consumed from the tag (see `anthropic/README.md`).

The version in the tag must match the `version` field in the component's `pyproject.toml`. The release workflow enforces this.
