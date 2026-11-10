"""Runtime configuration: ``SLASHID_*`` environment variables.

There is no push token here. The Event streaming token travels with each
webhook call, in the ``Authorization`` header the Portkey guardrail is
configured with, and goes on to SlashID unchanged. One deployment therefore
serves any number of connections, and holds no secret.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from slashid_ai_forwarder_core.config_base import BaseConfig


class Config(BaseConfig):
    # Unused: the caller's bearer token is the credential (see the module docstring).
    push_token: str = ""

    # What to answer when a request cannot be judged: no verdict from
    # SlashID in time, nobody named in the metadata, a request that is not
    # a chat completion.
    verdict_fail_mode: Literal["allow", "deny"] = "allow"
    # Portkey waits 3000 ms for a webhook unless the guardrail says otherwise.
    verdict_budget_ms: int = Field(2500, ge=100)
    # Judge and log, then answer allow.
    shadow_mode: bool = False
    # Send the fresh round's text (the prompt and tool results) with the
    # check. SlashID evaluates it and does not store it; rules on prompt
    # content cannot match without it.
    preflight_content: bool = True
    # Record each answered request as an AI invocation.
    push_events: bool = True
    # Portkey ``metadata`` keys naming who the request is for. ``_user`` is
    # Portkey's own key for the end user.
    user_metadata_key: str = Field("_user", min_length=1)
    agent_metadata_key: str = Field("agent", min_length=1)
    # Requests carry files inline; only their hashes go on to SlashID.
    max_body_bytes: int = Field(32 * 1024 * 1024, ge=1)


def load_config() -> Config:
    return Config()
