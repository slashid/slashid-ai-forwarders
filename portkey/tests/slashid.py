"""A stand-in for the SlashID API: answers the check, collects what it was sent."""

from __future__ import annotations

import json
from typing import Any

import httpx

ENDPOINT = "https://api.slashid.example"
PREFLIGHT = "/ip/nhi/events/ai-invocations/preflight"
PUSH = "/ip/nhi/events/ai-invocations"


class SlashID:
    def __init__(
        self,
        *deny_reasons: str,
        preflight_status: int = 200,
        push_status: int = 200,
        timeout: bool = False,
    ) -> None:
        self.deny_reasons = list(deny_reasons)
        self.preflight_status = preflight_status
        self.push_status = push_status
        self.timeout = timeout
        self.checked: list[dict[str, Any]] = []
        self.pushed: list[dict[str, Any]] = []
        self.tokens: list[str] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.tokens.append(request.headers.get("authorization", ""))
        body = json.loads(request.content)
        if request.url.path == PREFLIGHT:
            if self.timeout:
                raise httpx.ReadTimeout("too slow", request=request)
            self.checked.append(body)
            if self.preflight_status != 200:
                return httpx.Response(self.preflight_status)
            return httpx.Response(200, json={"deny_reasons": self.deny_reasons})
        assert request.url.path == PUSH, request.url.path
        if self.push_status != 200:
            return httpx.Response(self.push_status)
        self.pushed += body["events"]
        return httpx.Response(200, json={})
