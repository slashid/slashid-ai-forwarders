"""Converse-side translates.

Placeholder in 1.1 — the identity translate (ConverseResponse -> ConverseResponse)
is inlined into the mil_normalize dispatcher's format table via ``lambda r: r``.
Phase 2 fills in ``to_normalized(response: ConverseResponse) -> NormalizedInvocation``.
"""

from __future__ import annotations
