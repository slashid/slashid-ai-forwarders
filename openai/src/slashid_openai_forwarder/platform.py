"""The OpenAI forwarder's backends, built for the cloud it runs on: the
upload reader's watermark, the tick lease and the scheduler's identity."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from slashid_ai_forwarder_core import platform as platforms
from slashid_ai_forwarder_core.platform import CheckpointStore, SchedulerAuth, TickLease

from .config import Config

APP = "slashid_openai_forwarder"
# Document name in the checkpoint collection. Stable: renaming it silently
# starts the reader from a cold start.
UPLOADS = "compliance_conversation_messages"


@dataclass(frozen=True)
class Backends:
    checkpoint: CheckpointStore
    lease: TickLease
    tick_auth: SchedulerAuth


@contextlib.asynccontextmanager
async def open_backends(config: Config) -> AsyncIterator[Backends]:
    async with platforms.get(config.platform, **_options(config)) as platform:
        yield Backends(
            checkpoint=platform.checkpoint_store(
                collection=config.checkpoint_collection, document=UPLOADS
            ),
            lease=platform.tick_lease(collection=config.checkpoint_collection, document="tick"),
            tick_auth=platform.scheduler_auth(
                principal=config.tick_principal, audience=config.tick_audience
            ),
        )


def _options(config: Config) -> dict[str, Any]:
    if config.platform == "local":
        return {"app": APP, **({"path": config.data_dir} if config.data_dir else {})}
    return {"project": config.project_id, "firestore_database": config.database}
