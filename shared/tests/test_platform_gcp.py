"""``GcpPlatform``: what it hands out, without touching Google."""

from __future__ import annotations

from typing import Any

from fake_firestore import FakeFirestore
from slashid_ai_forwarder_core.platform import Checkpoint
from slashid_ai_forwarder_core.platform.gcp import GcpPlatform


def _platform(client: Any) -> GcpPlatform:
    platform = GcpPlatform(project="p", firestore_database="d")
    platform.__dict__["firestore"] = client  # what the cached property would build
    return platform


async def test_checkpoint_stores_share_the_client_and_keep_their_own_documents() -> None:
    client = FakeFirestore()
    platform = _platform(client)
    a = platform.checkpoint_store(collection="c", document="a")
    b = platform.checkpoint_store(collection="c", document="b")
    await a.save(Checkpoint(timestamp=None, id="x"))
    assert await b.load() == Checkpoint(timestamp=None, id=None)
    assert (await a.load()).id == "x"


def test_gcp_platform_exposes_one_firestore_client() -> None:
    assert hasattr(GcpPlatform, "firestore")
    assert not hasattr(GcpPlatform, "firestore_async")


async def test_scheduler_auth_without_a_principal_refuses_every_token() -> None:
    check = _platform(FakeFirestore()).scheduler_auth(principal=None, audience=None)
    assert await check("any-token") is False


async def test_get_returns_a_context_manager_yielding_the_platform() -> None:
    from slashid_ai_forwarder_core import platform

    async with platform.get("gcp", project="p", firestore_database="d") as built:
        assert isinstance(built, GcpPlatform)


def test_get_requires_the_options_the_factory_takes() -> None:
    import pytest

    from slashid_ai_forwarder_core import platform

    with pytest.raises(TypeError):
        platform.get("gcp")


def test_get_names_the_known_platforms_for_an_unknown_one() -> None:
    import pytest

    from slashid_ai_forwarder_core import platform

    with pytest.raises(ValueError, match=r"unknown platform 'azure'; known: \['gcp', 'local'\]"):
        platform.get("azure")
