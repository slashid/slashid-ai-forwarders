"""Anthropic Messages API wire shapes + normalization to Converse."""

# Temporary compat shim: re-export the Phase-1 (untyped) helper functions
# so callers that still import from this package top-level don't break.
# Chunks 3 and 4 migrate callers to `.schema` / `.normalize`; chunk 6
# deletes `_legacy.py`.
from ._legacy import (  # noqa: F401
    anthropic_message_to_converse,
    anthropic_stream_to_converse,
    anthropic_tools_to_converse_tool_config,
    extract_anthropic_stream_usage,
    looks_like_anthropic_message,
    looks_like_anthropic_stream,
)
