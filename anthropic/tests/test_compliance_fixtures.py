"""The double itself, because three modules trust it."""

from __future__ import annotations

import hashlib
import json

from tests.compliance_fixtures import (
    MARIA_BYTES,
    MARIA_ID,
    PAIRED,
    ROUTES,
    body,
    cases,
    transport,
)


def test_every_listing_endpoint_is_routed() -> None:
    assert {"/apps/sessions/local", "/apps/chats", "/activities"} <= set(ROUTES)


async def test_a_path_serves_its_own_body_and_an_unknown_path_404s() -> None:
    client, seen = transport()
    async with client:
        first = await client.get("https://api.anthropic.com/v1/compliance/apps/chats")
        missing = await client.get("https://api.anthropic.com/v1/compliance/nope")
    assert first.json() == body("chats_list.json")
    assert missing.status_code == 404
    assert len(seen) == 2


async def test_the_file_body_matches_the_listed_md5() -> None:
    # Not a mock agreeing with itself: the listing's md5 was recorded from
    # the tenant and these bytes are the frame's extracted text.
    listed = next(
        entry
        for message in body("chat_messages_1.json")["chat_messages"]
        for entry in (message.get("files") or [])
        if entry["id"] == MARIA_ID
    )
    assert hashlib.md5(MARIA_BYTES).hexdigest() == listed["md5"]
    assert len(MARIA_BYTES) == listed["size_bytes"]


def test_the_paired_frames_cover_the_recorded_sessions() -> None:
    # tests/fixtures/paired/session_N_frame_M.json was captured alongside
    # session_messages_N.json. Task 7.9 measures the address across them.
    frames = sorted(PAIRED.glob("*.json"))
    assert len(frames) == 14
    sessions = {json.loads(p.read_text())["session_id"] for p in frames}
    assert len(sessions) == 6


def test_the_recorded_rejections_are_the_five_the_client_avoids() -> None:
    rejected = [c for c in cases() if c["status"] >= 400]
    assert len(rejected) == 5
    params = " ".join(c["request"]["params"] for c in rejected)
    assert "updated_at.gte" in params  # chats, without order_by
    assert "order=asc" in params  # local sessions
    assert "order_by=updated_at" in params  # local sessions, the other one
    assert "created_at%5Bgte%5D" in params  # the bracketed form
    assert "organization_uuid" in params  # not a query parameter on any feed
    # The control the four above are read against: a compliance-base call
    # that does answer. The path confusion itself is organizations.json's.
    assert any(c["status"] == 200 and c["request"]["path"] == "/organizations" for c in cases())


def test_the_two_bases_answer_inverted_organization_paths() -> None:
    # Easy to get backwards in either direction, so all four combinations
    # are recorded and this is the one place that states which is which.
    answers = {
        (c["request"]["base"], c["request"]["path"]): c["status"]
        for c in cases("organizations.json")
    }
    assert answers == {
        ("v1/compliance", "/organizations"): 200,
        ("v1/compliance", "/organizations/me"): 404,
        ("v1", "/organizations"): 404,
        ("v1", "/organizations/me"): 200,
    }
