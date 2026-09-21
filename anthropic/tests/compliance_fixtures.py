"""The recorded compliance corpus, and one transport that serves it.

Recorded from the live tenant by ``scripts/record_compliance_fixtures.py``
and scrubbed; ``test_fixtures_scrubbed.py`` re-checks every byte, base64
included. Each file is ``{request: {method, path, params}, status, body}``,
except two that hold a ``cases`` list instead and are not routable:
``filters_rejected.json`` (the query vocabulary, same case shape) and
``organizations.json`` (``{note, cases}``, whose cases carry a ``base``
because the two bases answer inverted paths and path alone does not
identify them).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "compliance"
PAIRED = Path(__file__).parent / "fixtures" / "paired"

# The stored bytes of the smallest attachment in the corpus. Its md5 is the
# one `chat_messages_1.json` lists, and it is byte-identical to the text
# `frame_attachment.json` carries — which is the measured claim that plain
# text crosses the two surfaces unchanged.
MARIA_ID = "claude_file_01MpfHfLBGDPcEBwPQvWZMbY"
MARIA_BYTES = b"Maria tinha um carneirinho\n"


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def body(name: str) -> dict[str, Any]:
    return recorded(name)["body"]


def cases(name: str = "filters_rejected.json") -> list[dict[str, Any]]:
    """The multi-case fixtures. Two, and they answer different questions:
    ``filters_rejected.json`` is the query vocabulary, ``organizations.json``
    the path confusion between the two bases."""
    return recorded(name)["cases"]


def _routes() -> dict[str, str]:
    """Path to fixture, built from the corpus so it cannot drift from it.

    Only the single-response fixtures are routable. A ``cases`` fixture is
    skipped, and ``organizations.json`` is why that is a rule rather than
    an accident: its cases are distinguished by ``base``, not by path, so
    two of them share a path with two others and a path-keyed table would
    have to pick one and be wrong half the time.
    """
    table: dict[str, str] = {}
    for path in sorted(FIXTURES.iterdir()):
        payload = json.loads(path.read_text())
        request = payload.get("request")
        if isinstance(request, dict) and request.get("path"):
            table[request["path"]] = path.name
    return table


ROUTES = _routes()


def transport(
    *, files: Mapping[str, bytes] | None = None
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A client serving the corpus by path, and the requests it saw.

    By path and never by call order: one reader pass touches four
    endpoints in a data-dependent order. An unrouted path is a 404, which
    surfaces a wrong request as a failure instead of as a plausible body.
    """
    seen: list[httpx.Request] = []
    bodies = {MARIA_ID: MARIA_BYTES, **(files or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix("/v1/compliance")
        if "/files/" in path:
            file_id = path.split("/files/")[1].split("/")[0]
            if file_id not in bodies:
                return httpx.Response(404, json={"type": "error"})
            return httpx.Response(200, content=bodies[file_id])
        name = ROUTES.get(path)
        if name is None:
            return httpx.Response(404, json={"type": "error", "path": path})
        entry = recorded(name)
        return httpx.Response(entry["status"], json=entry["body"])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen
