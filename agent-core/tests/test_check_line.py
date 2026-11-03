"""The ``check`` line: one per check sent to SlashID or left without an answer, fixed words only."""

import logging
import types
from collections.abc import Iterator

import pytest
from pydantic import JsonValue

from helpers import LOGGER, Load, body_of, checker_of, ctx, events, make_runtime
from slashid_agentcore_forwarder import gateway, harness, pipeline
from slashid_agentcore_forwarder.checks import Outcome
from slashid_agentcore_forwarder.runtime import Runtime

OUTCOMES = {
    "allow": Outcome("allow"),
    "deny": Outcome("deny", ("SECRET-REASON",)),
    "no_answer": Outcome("no_answer", why="timeout"),
}
# (fixture, surface, check) -- one call check and one result check per surface
CASES = [
    ("harness_before_tool_call", "harness", "call"),
    ("harness_after_tool_call", "harness", "result"),
    ("gateway_request", "gateway", "call"),
    ("gateway_response", "gateway", "result"),
]


class Clock:
    """A monotonic clock that moves ``step`` seconds per reading."""

    def __init__(self, step: float) -> None:
        self.now = 1000.0
        self.step = step

    def __call__(self) -> float:
        now = self.now
        self.now += self.step
        return now


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    fake = Clock(0.1234)
    monkeypatch.setattr(pipeline, "time", types.SimpleNamespace(monotonic=fake))
    yield fake


async def run(runtime: Runtime, payload: dict[str, JsonValue], surface: str) -> JsonValue:
    if surface == "harness":
        return await harness.handle(runtime, payload)
    return await gateway.handle(runtime, payload, ctx())


@pytest.mark.parametrize("outcome", OUTCOMES)
@pytest.mark.parametrize(("fixture", "surface", "check"), CASES)
async def test_one_line_per_check_with_exactly_its_keys(
    load: Load,
    caplog: pytest.LogCaptureFixture,
    fixture: str,
    surface: str,
    check: str,
    outcome: str,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime(OUTCOMES[outcome])
    await run(runtime, load(fixture), surface)
    assert len(checker_of(runtime).invocations) == 1
    assert events(caplog, "check") == [
        {
            "event": "check",
            "surface": surface,
            "check": check,
            "outcome": outcome,
            "latency_ms": 123,
        }
    ]


@pytest.mark.parametrize(("step", "expected"), [(0.0, 0), (0.0004, 0), (0.0016, 1), (2.5, 2500)])
async def test_latency_is_whole_milliseconds_from_the_fake_clock(
    load: Load, caplog: pytest.LogCaptureFixture, clock: Clock, step: float, expected: int
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    clock.step = step
    await run(make_runtime(), load("harness_before_tool_call"), "harness")
    [line] = events(caplog, "check")
    latency = line["latency_ms"]
    assert type(latency) is int
    assert latency == expected >= 0


async def test_latency_starts_at_the_time_the_checker_is_given(load: Load) -> None:
    runtime = make_runtime()
    await run(runtime, load("gateway_request"), "gateway")
    assert checker_of(runtime).started == [1000.0]


async def test_a_no_answer_check_line_names_no_reason(
    load: Load, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    runtime = make_runtime(OUTCOMES["no_answer"], verdict_fail_mode="deny")
    await run(runtime, load("gateway_request"), "gateway")
    [line] = [r.getMessage() for r in caplog.records if '"event": "check"' in r.getMessage()]
    assert "timeout" not in line
    assert len(events(caplog, "fail_mode")) == 1


async def test_no_check_line_without_a_check(load: Load, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER)
    await run(make_runtime(), load("gateway_request_notification"), "gateway")
    await run(make_runtime(), load("gateway_response_notification"), "gateway")
    await run(make_runtime(), load("gateway_response_streaming"), "gateway")
    runtime = make_runtime(verdict_fail_mode="deny")
    payload = load("gateway_request")
    del body_of(payload)["params"]
    await run(runtime, payload, "gateway")  # unreadable: it has its own lines
    assert events(caplog, "check") == []
