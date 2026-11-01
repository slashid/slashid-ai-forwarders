"""The ChatGPT Enterprise Compliance API, the three calls this forwarder makes.

Every route is under ``/compliance/workspaces/{workspace_id}`` on
``api.chatgpt.com``. Paging follows each listing's own cursor: a user id for
the users listing, ``last_end_time`` for the logs listing.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from .config import Config

log = logging.getLogger(__name__)

USERS_PAGE = 200
LOGS_PAGE = 100
CONVERSATION_MESSAGE = "CONVERSATION_MESSAGE"


class ComplianceError(Exception):
    """A call failed; the message carries the route and status, never the key."""


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class WorkspaceUser(_Lenient):
    id: str
    email: str | None = None


class _UserPage(_Lenient):
    data: list[WorkspaceUser]
    has_more: bool = False
    last_id: str | None = None


class LogFile(_Lenient):
    id: str
    end_time: datetime | None = None


class LogPage(_Lenient):
    data: list[LogFile]
    has_more: bool = False
    # Null on an empty page.
    last_end_time: datetime | None = None


class ComplianceClient:
    def __init__(self, config: Config, http: httpx.AsyncClient) -> None:
        assert config.compliance_key and config.workspace_id
        self._base = f"{config.compliance_base_url}/compliance/workspaces/{config.workspace_id}"
        self._headers = {"Authorization": f"Bearer {config.compliance_key}"}
        self._timeout = config.compliance_timeout_seconds
        self._http = http

    async def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        try:
            response = await self._http.get(
                f"{self._base}{path}",
                params=params,
                headers=self._headers,
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ComplianceError(f"GET {path}: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise ComplianceError(f"GET {path}: HTTP {response.status_code}")
        return response

    async def users(self) -> list[WorkspaceUser]:
        """Every user in the workspace, all pages."""
        out: list[WorkspaceUser] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": USERS_PAGE}
            if after:
                params["after"] = after
            page = _UserPage.model_validate_json((await self._get("/users", params)).content)
            out.extend(page.data)
            after = page.last_id or (page.data[-1].id if page.data else None)
            if not page.has_more or not after:
                return out

    async def log_files(self, *, after: datetime) -> LogPage:
        """One page of conversation-message log files ending after ``after``,
        oldest first."""
        params = {
            "event_type": CONVERSATION_MESSAGE,
            "after": after.isoformat().replace("+00:00", "Z"),
            "limit": LOGS_PAGE,
        }
        return LogPage.model_validate_json((await self._get("/logs", params)).content)

    async def log_lines(self, file_id: str) -> list[bytes]:
        """The JSONL file's lines. The route answers 307 to a signed URL,
        which is fetched without our key."""
        response = await self._get(f"/logs/{file_id}", {})
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise ComplianceError(f"GET /logs/{file_id}: redirect without a location")
            try:
                response = await self._http.get(location, timeout=self._timeout)
            except httpx.HTTPError as exc:
                raise ComplianceError(f"download {file_id}: {type(exc).__name__}") from None
            if response.status_code >= 400:
                raise ComplianceError(f"download {file_id}: HTTP {response.status_code}")
        return [line for line in response.content.splitlines() if line.strip()]
