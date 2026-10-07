"""Puts the core inside the wheel.

The wheel is the only thing published, so it carries ``slashid_ai_forwarder_core``
rather than depending on a second package. Only a real wheel gets it: an editable
install would copy it into site-packages, where it would shadow ``shared/src``
for the whole workspace.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class VendorCore(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version != "standard":
            return
        core = Path(self.root).parent / "shared" / "src" / "slashid_ai_forwarder_core"
        if not core.is_dir():
            raise RuntimeError(f"cannot vendor the core: {core} does not exist")
        build_data["force_include"][str(core)] = "slashid_ai_forwarder_core"
