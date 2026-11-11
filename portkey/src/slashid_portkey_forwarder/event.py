"""A webhook body → the events SlashID judges and records.

Before the model call there is one event: who is asking, which model, and
the fresh round (the prompt, tool results and inline files since the last
answer). After it there are two: the tool calls the model asked for, judged
before the application runs any, and the completed invocation, recorded.

Portkey hands every provider's request and response over in OpenAI Chat
Completions form, so one normalizer covers OpenAI, Anthropic and Gemini.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from slashid_ai_forwarder_core.events import (
    AIGatewayIdentityDetails,
    AIInvocationObservedV1,
    AIModel,
    AIToolUse,
    EventEnvelope,
    build_event_from_normalized,
    preflight_arguments,
)
from slashid_ai_forwarder_core.normalize.finalize import finalize
from slashid_ai_forwarder_core.normalize.openai.chat.normalize import (
    chat_request_to_normalized_invocation,
    chat_to_normalized_invocation,
    to_normalized,
)
from slashid_ai_forwarder_core.normalize.openai.chat.schema import (
    ChatChoice,
    ChatCompletion,
    ChatRequest,
)

from .config import Config
from .hook import Hook

PARSED_AS = "portkey-openai-chat"
CHAT = "chatComplete"


@dataclass(frozen=True)
class After:
    """``judged`` is None when the model asked for no tool call."""

    judged: AIInvocationObservedV1 | None
    record: AIInvocationObservedV1


def identity_of(metadata: Mapping[str, object], config: Config) -> AIGatewayIdentityDetails | None:
    """Who the request is for, from the caller's metadata; None when it names nobody."""
    user = _text(metadata.get(config.user_metadata_key))
    agent = _text(metadata.get(config.agent_metadata_key))
    if not (user or agent):
        return None
    is_email = user is not None and "@" in user
    return AIGatewayIdentityDetails(
        user_email=user.lower() if user and is_email else None,
        user_id=user if user and not is_email else None,
        agent_id=agent,
    )


def _text(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


async def before_event(
    hook: Hook, identity: AIGatewayIdentityDetails, *, config: Config, now: datetime | None = None
) -> AIInvocationObservedV1 | None:
    """The request about to be sent; None when it is not a chat completion."""
    request = _chat_request(hook)
    if request is None:
        return None
    normalized = finalize(
        await chat_request_to_normalized_invocation(request, config=config), config=config
    )
    envelope = _envelope(hook, request, identity, _request_key(hook, identity), now)
    # The text is for the check alone. The record built after the answer
    # follows ``include_raw_content``.
    judged_with = config.model_copy(update={"include_raw_content": config.preflight_content})
    return _unanswered(await build_event_from_normalized(normalized, envelope, config=judged_with))


async def after_events(
    hook: Hook, identity: AIGatewayIdentityDetails, *, config: Config, now: datetime | None = None
) -> After | None:
    """The answered request; None when it is not a chat completion."""
    request = _chat_request(hook)
    if request is None:
        return None
    response = _completion(hook)
    key = _request_key(hook, identity)
    if response is None:
        # A streamed answer: Portkey does not hand its body over. Nothing
        # distinguishes this call from a repeat of the same request.
        normalized = await chat_request_to_normalized_invocation(request, config=config)
        suffix = uuid.uuid4().hex[:8]
    else:
        normalized = await chat_to_normalized_invocation(request, response, config=config)
        suffix = hashlib.sha256(response.id.encode()).hexdigest()[:8]
    envelope = _envelope(hook, request, identity, f"{key}_{suffix}", now)
    envelope.tokens = normalized.tokens
    record = await build_event_from_normalized(
        finalize(normalized, config=config), envelope, config=config
    )
    if response is None:
        record = _unanswered(record)
    requested = await _requested(request, response, record, envelope, config)
    if not requested:
        return After(judged=None, record=record)
    judged = AIInvocationObservedV1(
        request_id=record.request_id,
        timestamp=record.timestamp,
        identity_details=identity,
        model=record.model,
        parsed_as=PARSED_AS,
        requested_tool_uses=requested,
        available_tools=record.available_tools,
        available_tool_servers=record.available_tool_servers,
    )
    return After(judged=judged, record=record)


def _unanswered(event: AIInvocationObservedV1) -> AIInvocationObservedV1:
    """The shared builder writes an empty answer's hash and an ``unknown``
    stop reason; an event with no answer carries neither."""
    return event.model_copy(update={"output": None, "stop_reason": None})


async def _requested(
    request: ChatRequest,
    response: ChatCompletion | None,
    record: AIInvocationObservedV1,
    envelope: EventEnvelope,
    config: Config,
) -> list[AIToolUse]:
    """Every choice's tool calls, each with the arguments the model wrote:
    the application may run any of them."""
    if response is None:
        return []
    requested: list[AIToolUse] = []
    for index, choice in enumerate(response.choices):
        if index == 0:
            uses = record.requested_tool_uses or []
        else:
            only = response.model_copy(update={"choices": [choice]})
            event = await build_event_from_normalized(
                to_normalized(request, only), envelope, config=config
            )
            uses = event.requested_tool_uses or []
        arguments = _arguments(choice)
        requested += [
            use.model_copy(update={"arguments": arguments.get(use.tool_use_id or "")})
            for use in uses
        ]
    return requested


def _arguments(choice: ChatChoice) -> dict[str, dict[str, object] | None]:
    out: dict[str, dict[str, object] | None] = {}
    for call in choice.message.tool_calls:
        try:
            out[call.id] = preflight_arguments(json.loads(call.function.arguments))
        except json.JSONDecodeError:
            out[call.id] = None
    return out


def _chat_request(hook: Hook) -> ChatRequest | None:
    if hook.request_type != CHAT:
        return None
    try:
        return ChatRequest.model_validate(hook.request.body)
    except ValidationError:
        return None


def _completion(hook: Hook) -> ChatCompletion | None:
    try:
        return ChatCompletion.model_validate(hook.response.body)
    except ValidationError:
        return None


def _envelope(
    hook: Hook,
    request: ChatRequest,
    identity: AIGatewayIdentityDetails,
    request_id: str,
    now: datetime | None,
) -> EventEnvelope:
    return EventEnvelope(
        request_id=request_id,
        timestamp=(now or datetime.now(UTC)).isoformat(),
        identity_details=identity,
        model=_model(request.model, hook.provider),
        parsed_as=PARSED_AS,
    )


def _model(raw: str | None, provider: str | None) -> AIModel:
    """Portkey's model catalog prefixes the model with ``@<integration>/``;
    a policy names the model itself."""
    name = raw or "unknown"
    if name.startswith("@") and "/" in name:
        name = name.split("/", 1)[1]
    return AIModel(id=name, provider=provider or None, raw_model_id=raw)


def _request_key(hook: Hook, identity: AIGatewayIdentityDetails) -> str:
    """Stable for one request by one caller: Portkey gives a webhook no
    request id, and the before and after calls must name the same request."""
    canonical = json.dumps(
        [identity.model_dump(mode="json", exclude_none=True), hook.request.body],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "pk_" + hashlib.sha256(canonical.encode()).hexdigest()[:32]
