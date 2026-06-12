"""Bedrock foundation-model catalog: maps modelId → ARN + display name + provider.

MIL records carry `modelId` in several forms:
  - canonical ID:               "amazon.nova-micro-v1:0"
  - cross-region profile ID:    "us.amazon.nova-micro-v1:0"
  - APAC profile ID:            "apac.amazon.nova-micro-v1:0"
  - inference-profile ARN:
    "arn:aws:bedrock:ap-northeast-1:851...:inference-profile/apac.amazon.nova-micro-v1:0"

model.id in the pushed event is:
  - the raw modelId when it is already an ARN
  - the foundation-model ARN from the catalog otherwise
  - the raw modelId as fallback when the catalog has no entry

The catalog is loaded once per (region, Lambda container) and reused across
warm invocations. A failed load leaves the catalog empty so downstream code
degrades gracefully.
"""

from __future__ import annotations

import logging
import re
from typing import TypedDict

log = logging.getLogger(__name__)

# region → {canonicalModelId → ModelInfo}
_catalogs: dict[str, dict[str, ModelInfo]] = {}


class ModelInfo(TypedDict):
    arn: str
    name: str
    provider: str


# Matches "arn:aws[...]:bedrock:<region>:<account>:inference-profile/<model-id>"
_PROFILE_ARN_RE = re.compile(r"arn:aws[^:]*:bedrock:[^:]+:[^:]*:inference-profile/(.+)$")
# Geo-prefix: 2-4 lowercase letters followed by a dot ("us.", "eu.", "ap.", "apac.", etc.)
# Provider names ("amazon.", "anthropic.") are 5+ chars and must not be stripped.
_GEO_PREFIX_RE = re.compile(r"^[a-z]{2,4}\.")


def _canonical_id(raw: str) -> str:
    """Reduce any modelId form to the bare canonical model ID for catalog lookup.

    "arn:aws:bedrock:ap-northeast-1:851...:inference-profile/apac.amazon.nova-micro-v1:0"
      → "amazon.nova-micro-v1:0"
    "apac.amazon.nova-micro-v1:0" → "amazon.nova-micro-v1:0"
    "us.amazon.nova-micro-v1:0"   → "amazon.nova-micro-v1:0"
    "amazon.nova-micro-v1:0"      → "amazon.nova-micro-v1:0"
    """
    m = _PROFILE_ARN_RE.match(raw)
    if m:
        raw = m.group(1)
    return _GEO_PREFIX_RE.sub("", raw, count=1)


def _load(region: str) -> dict[str, ModelInfo]:
    try:
        import boto3

        resp = boto3.client("bedrock", region_name=region).list_foundation_models()
        return {
            m["modelId"]: ModelInfo(
                arn=m["modelArn"],
                name=m["modelName"],
                provider=m["providerName"],
            )
            for m in resp.get("modelSummaries", [])
        }
    except Exception as e:
        log.warning("failed to load Bedrock model catalog for region %s: %s", region, e)
        return {}


def get_model_info(raw_model_id: str, region: str) -> ModelInfo | None:
    """Return ModelInfo for `raw_model_id` in `region`, or None if not found."""
    if region not in _catalogs:
        _catalogs[region] = _load(region)
    catalog = _catalogs[region]
    canonical = _canonical_id(raw_model_id)
    return catalog.get(canonical) or catalog.get(raw_model_id) or None


def reset_catalog() -> None:
    """Clear all cached catalogs. Used in tests to prevent cross-test leakage."""
    _catalogs.clear()
