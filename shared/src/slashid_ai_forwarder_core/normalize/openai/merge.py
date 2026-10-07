"""Consecutive same-role merging shared by the OpenAI normalizers."""

from __future__ import annotations

from typing import Literal

from ..normalized.types import NormalizedContent, NormalizedMessage

Role = Literal["system", "user", "assistant"]


def merge_same_role(mapped: list[tuple[Role, list[NormalizedContent]]]) -> list[NormalizedMessage]:
    """Consecutive same-role items share a message; a compaction is always alone,
    so it never folds the round before it into its own."""
    groups: list[tuple[Role, list[NormalizedContent]]] = []
    for role, blocks in mapped:
        if (
            groups
            and groups[-1][0] == role
            and not (_is_compaction(blocks) or _is_compaction(groups[-1][1]))
        ):
            groups[-1][1].extend(blocks)
        else:
            groups.append((role, list(blocks)))
    return [NormalizedMessage(role=role, content=blocks) for role, blocks in groups]


def _is_compaction(blocks: list[NormalizedContent]) -> bool:
    return any(b.kind == "compaction" for b in blocks)
