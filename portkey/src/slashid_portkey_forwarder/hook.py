"""The body Portkey's webhook guardrail posts.

Portkey sends its whole hook context: the request as the caller wrote it,
the provider's response once there is one, the provider, the kind of
request, the caller's metadata and which side of the model call this is.
Request headers are stripped before it leaves the gateway.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError


def _none_to_empty(value: object) -> object:
    return {} if value is None else value


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class HookRequest(_Wire):
    body: Any = Field(None, alias="json")


class HookResponse(_Wire):
    # Empty before the model has answered, and for a streamed answer.
    body: Any = Field(None, alias="json")


class Hook(_Wire):
    request: HookRequest = Field(default_factory=HookRequest)
    response: HookResponse = Field(default_factory=HookResponse)
    provider: str | None = None
    request_type: str | None = Field(None, alias="requestType")
    metadata: Annotated[dict[str, Any], BeforeValidator(_none_to_empty)] = Field(
        default_factory=dict
    )
    event_type: Literal["beforeRequestHook", "afterRequestHook"] = Field(alias="eventType")


def parse_hook(raw: object) -> Hook | None:
    try:
        return Hook.model_validate(raw)
    except ValidationError:
        return None
