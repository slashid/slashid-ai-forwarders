# Build context is the repository root: the image needs both ``shared/``
# and ``openai/`` from the uv workspace.
#
#   docker build -f openai/Dockerfile .
#   (Cloud Build config and Terraform: a follow-up, as for anthropic/deploy/.)

FROM cgr.dev/chainguard/wolfi-base:latest AS builder

USER root
RUN apk add --no-cache python-3.14 uv

WORKDIR /build
ENV UV_PROJECT_ENVIRONMENT=/venv \
    UV_PYTHON=/usr/bin/python3.14 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# Dependency layer first: only the manifests, so source edits don't
# re-resolve. ``--no-install-workspace`` keeps our own packages out of it.
COPY pyproject.toml uv.lock ./
COPY shared/pyproject.toml shared/
COPY bedrock/pyproject.toml bedrock/
COPY vertex/pyproject.toml vertex/
COPY openai/pyproject.toml openai/
RUN uv sync --frozen --no-dev --no-install-workspace --package slashid-ai-forwarder-openai

COPY shared/src shared/src
COPY openai/src openai/src
RUN uv sync --frozen --no-dev --no-editable --package slashid-ai-forwarder-openai


FROM cgr.dev/chainguard/wolfi-base:latest

USER root
RUN apk add --no-cache python-3.14 && rm -rf /var/cache/apk/*

ENV PYTHONUNBUFFERED=1 \
    PATH="/venv/bin:$PATH" \
    PORT=8080

COPY --from=builder /venv /venv

# State for the local platform; harmless where the platform is gcp.
RUN mkdir -p /data && chown nonroot:nonroot /data
ENV SLASHID_DATA_DIR=/data
VOLUME /data

USER nonroot
WORKDIR /app
EXPOSE 8080

ENTRYPOINT []
CMD ["sh", "-c", "exec uvicorn slashid_openai_forwarder.main:app --factory --host=0.0.0.0 --port=${PORT} --forwarded-allow-ips=* --no-server-header"]
