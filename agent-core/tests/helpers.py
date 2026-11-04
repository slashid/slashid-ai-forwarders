"""What the adapter tests share: the fake checker and roles, the runtime and the log reader."""

import json
import time
from collections.abc import Callable

import pytest
from pydantic import JsonValue
from slashid_ai_forwarder_core.events import AIInvocationObservedV1, AWSIdentityDetails

from slashid_agentcore_forwarder.cache import TtlCache
from slashid_agentcore_forwarder.checks import Outcome
from slashid_agentcore_forwarder.config import Config
from slashid_agentcore_forwarder.deny import derive_key
from slashid_agentcore_forwarder.payloads import ClientContext
from slashid_agentcore_forwarder.roles import HarnessDetails
from slashid_agentcore_forwarder.runtime import Runtime
from slashid_agentcore_forwarder.tool_call import CachedInput

LOGGER = "slashid_agentcore_forwarder"
GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-2:123456789012:gateway/gw-EXAMPLE"
RID = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "arn:aws:iam::123456789012:role/AgentCore-example-ApplicationRuntimeAgentRole"
GATEWAY_ROLE = "arn:aws:iam::123456789012:role/gateway-role"
HARNESS_ROLE = "arn:aws:iam::123456789012:role/harness-role"

Load = Callable[[str], dict[str, JsonValue]]


def make_config(**overrides: object) -> Config:
    return Config.model_validate(
        {"endpoint": "http://test", "push_token": "push-token", **overrides}
    )


class FakeChecker:
    """Records what it is asked and answers from a script (allow when it runs out)."""

    def __init__(self, *outcomes: Outcome) -> None:
        self.outcomes = list(outcomes)
        self.invocations: list[AIInvocationObservedV1] = []
        self.started: list[float] = []

    async def check(self, invocation: AIInvocationObservedV1, *, started: float) -> Outcome:
        self.invocations.append(invocation)
        self.started.append(started)
        return self.outcomes.pop(0) if self.outcomes else Outcome("allow")


class FakeRoles:
    """``looked_up`` holds ``(kind, arn)`` pairs, ``looked_up_at`` the ``monotonic()`` of each."""

    def __init__(
        self,
        role: str | None = HARNESS_ROLE,
        gateway: str | None = GATEWAY_ROLE,
        claimed: str | None = PRINCIPAL,
    ) -> None:
        self.role = role
        self.gateway = gateway
        self.claimed = claimed
        self.details: HarnessDetails | None = HarnessDetails(name="hdemo", version="3")
        self.looked_up: list[tuple[str, str]] = []
        self.looked_up_at: list[float] = []

    async def harness_role(self, harness_arn: str) -> str | None:
        self.looked_up.append(("harness", harness_arn))
        self.looked_up_at.append(time.monotonic())
        return self.role

    def harness_details(self, harness_arn: str) -> HarnessDetails | None:
        return self.details

    async def gateway_role(self, gateway_arn: str) -> str | None:
        self.looked_up.append(("gateway", gateway_arn))
        self.looked_up_at.append(time.monotonic())
        return self.gateway

    async def claimed_harness_role(self, harness_arn: str, *, fresh: bool = False) -> str | None:
        self.looked_up.append(("claimed", harness_arn))
        self.looked_up_at.append(time.monotonic())
        return self.claimed


def make_runtime(*outcomes: Outcome, roles: FakeRoles | None = None, **config: object) -> Runtime:
    cfg = make_config(**config)
    return Runtime(
        config=cfg,
        key=derive_key(cfg.push_token),
        checker=FakeChecker(*outcomes),
        roles=roles or FakeRoles(),
        inputs=TtlCache[CachedInput](ttl_s=600, max_entries=1000),
    )


def checker_of(runtime: Runtime) -> FakeChecker:
    assert isinstance(runtime.checker, FakeChecker)
    return runtime.checker


def looked_up(runtime: Runtime) -> list[tuple[str, str]]:
    assert isinstance(runtime.roles, FakeRoles)
    return runtime.roles.looked_up


def identity_of(event: AIInvocationObservedV1) -> AWSIdentityDetails:
    assert isinstance(event.identity_details, AWSIdentityDetails)
    return event.identity_details


def events(caplog: pytest.LogCaptureFixture, name: str) -> list[dict[str, object]]:
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.name == LOGGER]
    return [line for line in lines if line["event"] == name]


def ctx(**overrides: str | None) -> ClientContext:
    return ClientContext.model_validate(
        {"GATEWAY_ARN": GATEWAY_ARN, "REQUEST_ID": RID, **overrides}
    )


def obj(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def mcp_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(payload["mcp"])


def request_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(mcp_of(payload)["gatewayRequest"])


def body_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(request_of(payload)["body"])


def params_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(body_of(payload)["params"])


def response_of(payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return obj(mcp_of(payload)["gatewayResponse"])


def with_result(
    load: Load, result: JsonValue, name: str = "gateway_response"
) -> dict[str, JsonValue]:
    payload = load(name)
    obj(response_of(payload)["body"])["result"] = result
    return payload


def allow_request(body: JsonValue) -> JsonValue:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": body}},
    }


def deny_body(rpc_id: JsonValue, text: str) -> JsonValue:
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"isError": True, "content": [{"type": "text", "text": text}]},
                },
            }
        },
    }


def denied_text(answer: JsonValue) -> str:
    """The text of a deny answer, whose shape is asserted along the way."""
    response = obj(obj(obj(answer)["mcp"])["transformedGatewayResponse"])
    assert response["statusCode"] == 200
    body = obj(response["body"])
    assert body["jsonrpc"] == "2.0"
    result = obj(body["result"])
    assert result["isError"] is True
    [block] = result["content"] if isinstance(result["content"], list) else []
    text = obj(block)["text"]
    assert isinstance(text, str)
    return text
