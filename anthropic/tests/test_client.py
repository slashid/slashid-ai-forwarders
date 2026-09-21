"""The three feeds, against the recorded corpus."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from slashid_ai_forwarder_core.testing import yaml_pytest

from slashid_anthropic_forwarder.compliance.client import (
    ComplianceClient,
    ComplianceError,
    chat_session_id,
    created_at,
    decode_session_id,
    provenance_type,
)
from tests.compliance_fixtures import MARIA_BYTES, MARIA_ID, body, cases, transport

SINCE = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


def a_client() -> tuple[ComplianceClient, list[httpx.Request]]:
    client, seen = transport()
    return ComplianceClient(client, api_key="sk-ant-api01-fixture"), seen


@yaml_pytest(filename="test_feed_vocabulary.yaml")
async def test_feed_vocabulary(feed: str, params: dict[str, str]) -> None:
    client, seen = a_client()
    await _drain(client, feed)
    query = dict(seen[0].url.params)
    assert {k: query.get(k) for k in params} == params
    # Nothing a feed rejects may leak in from another feed's vocabulary —
    # `order` on chats and either ordering on sessions are recorded 4xx.
    assert set(query) <= set(params) | {"limit", "after_id", "page"}


async def test_requests_carry_the_key_and_the_version() -> None:
    client, seen = a_client()
    [_ async for _ in client.iter_activities(since=SINCE)]
    assert seen[0].headers["x-api-key"] == "sk-ant-api01-fixture"
    assert seen[0].headers["anthropic-version"] == "2023-06-01"


async def test_activities_stop_when_has_more_is_false() -> None:
    client, seen = a_client()
    rows = [row async for row in client.iter_activities(since=SINCE)]
    assert len(rows) == len(body("activities.json")["data"])
    assert len(seen) == 1


async def test_a_second_page_is_fetched_with_after_id() -> None:
    # No second page was recorded — the tenant's whole window fits one —
    # so this pair is synthetic, and it exists only to pin the cursor
    # parameter the recorded body names (`last_id`) to the one the
    # request sends (`after_id`).
    pages = [
        {"data": [{"id": "a", "type": "x"}], "has_more": True, "last_id": "cursor-1"},
        {"data": [{"id": "b", "type": "x"}], "has_more": False, "last_id": "cursor-2"},
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[min(len(seen) - 1, 1)])

    client = ComplianceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_key="k"
    )
    rows = [row async for row in client.iter_activities(since=SINCE)]
    assert [r["id"] for r in rows] == ["a", "b"]
    assert dict(seen[1].url.params)["after_id"] == "cursor-1"


async def test_local_sessions_drain_is_marked_incomplete_at_the_cap() -> None:
    client, _ = a_client()
    drain = await client.drain_local_sessions(since=SINCE, limit=2)
    assert len(drain.sessions) == 2
    assert drain.complete is False


async def test_a_completed_drain_says_so() -> None:
    client, _ = a_client()
    drain = await client.drain_local_sessions(since=SINCE, limit=500)
    assert len(drain.sessions) == len(body("sessions_list.json")["data"])
    assert drain.complete is True


async def test_decode_session_id_yields_the_frames_session_id() -> None:
    decoded = {decode_session_id(s["id"]) for s in body("sessions_list.json")["data"]}
    # The paired frames carry exactly these as `session_id`; that is the
    # whole join between a captured delivery and a stored transcript.
    assert "00000001-0000-4000-8000-000000000000" in decoded
    assert None not in decoded


def test_decode_session_id_survives_a_missing_pad_and_refuses_junk() -> None:
    assert decode_session_id("clls_not-base64") is None
    assert decode_session_id("sess_01ABC") is None


def test_a_chats_session_id_is_the_uuid_its_href_ends_with() -> None:
    # The other half of the same question, and the soft join's hard half:
    # measured on every claude.ai conversation in the tenant, a frame's
    # `session_id` is this uuid — three of three. It is NOT the
    # `claude_chat_…` id, which no frame carries.
    for chat in body("chats_list.json")["data"]:
        assert chat_session_id(chat) == chat["href"].rsplit("/", 1)[-1]
        assert chat_session_id(chat) != chat["id"]
    assert chat_session_id({}) is None


def test_created_at_parses_both_recorded_spellings() -> None:
    # The feeds spell the same instant two ways — an activity's offset and
    # a message's `Z` — which is also why a timestamp comparison cannot be
    # left to string order.
    activity = body("activities.json")["data"][0]
    message = body("chat_messages_1.json")["chat_messages"][0]
    assert created_at(activity) is not None
    assert created_at(message) is not None
    assert created_at({"created_at": "not a time"}) is None
    assert created_at({}) is None


def test_provenance_is_an_object_not_a_string() -> None:
    assert provenance_type({"provenance": {"type": "client_asserted"}}) == "client_asserted"
    assert provenance_type(
        {"provenance": {"type": "content_unavailable", "reason": "oversize"}}
    ) == ("content_unavailable")
    # Unknown values are tolerated by the schema and by us: skipped, never
    # rejected. A bare string was never the shape, and `None` is the shape
    # a produced local-session turn actually has.
    assert provenance_type({"provenance": {"type": "future_kind"}}) == "future_kind"
    assert provenance_type({"provenance": None}) is None
    assert provenance_type({}) is None


async def test_a_chat_transcript_lives_under_chat_messages_not_data() -> None:
    # The chats endpoint answers the chat object, so its turns are under
    # `chat_messages`. Reading `data` yields nothing and says nothing.
    client, _ = a_client()
    chat = body("chats_list.json")["data"][1]
    messages = await client.chat_messages(chat["id"])
    assert messages
    assert messages == body("chat_messages_2.json")["chat_messages"]


async def test_the_chat_object_is_available_for_its_model() -> None:
    # No chat message carries a model; the chat does, and Reader B needs
    # it, so the client hands back both halves.
    client, _ = a_client()
    chat = body("chats_list.json")["data"][1]
    fetched = await client.chat(chat["id"])
    assert fetched["model"] == body("chat_messages_2.json")["model"]


async def test_a_session_transcript_comes_back_whole() -> None:
    client, _ = a_client()
    session = body("sessions_list.json")["data"][0]
    messages = await client.session_messages(session["id"])
    assert len(messages) == len(body("session_messages_1.json")["data"])


async def test_tool_caps_ride_on_a_transcript_request() -> None:
    client, seen = a_client()
    session = body("sessions_list.json")["data"][0]
    await client.session_messages(session["id"], tool_block_bytes=-1)
    query = dict(seen[0].url.params)
    assert query["tool_result_max_bytes"] == "-1"
    assert query["tool_use_input_max_bytes"] == "-1"


async def test_file_content_is_the_whole_body() -> None:
    client, _ = a_client()
    assert await client.file_content(MARIA_ID) == MARIA_BYTES


async def test_non_2xx_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate_limited"})

    client = ComplianceClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), api_key="k"
    )
    with pytest.raises(ComplianceError):
        [_ async for _ in client.iter_activities(since=SINCE)]


def test_the_rejections_the_vocabulary_avoids_were_recorded() -> None:
    assert len([c for c in cases() if c["status"] >= 400]) == 5


async def _drain(client: ComplianceClient, feed: str) -> None:
    if feed == "activities":
        [_ async for _ in client.iter_activities(since=SINCE)]
    elif feed == "chats":
        [_ async for _ in client.iter_chats(since=SINCE)]
    else:
        await client.drain_local_sessions(since=SINCE, limit=10)
