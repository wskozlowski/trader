"""Server-held reviews tied to one workspace, selection and draft revision."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...errors import TraderError
from ...ui_api.commands import PreparedCommand, submit_prepared
from ..runtime import TraderRuntime


@dataclass
class CommandController:
    runtime: TraderRuntime
    revision: int = 0
    prepared: PreparedCommand | None = field(default=None, repr=False)
    active: bool = True
    busy: bool = False

    def invalidate(self) -> None:
        self.revision += 1
        self.prepared = None

    def leave(self) -> None:
        self.active = False
        self.invalidate()

    async def execute[T](self, command: Callable[[], T]) -> T:
        if not self.active or not self.runtime.accepting_commands:
            raise TraderError("WORKSPACE_CLOSED", "workspace is no longer active")
        # The executor owns dispatched work. Cancelling a browser waiter cannot cancel it.
        future = self.runtime.command_executor.submit(command)
        return await asyncio.shield(asyncio.wrap_future(future))

    async def prepare(self, command: Callable[[], PreparedCommand]) -> PreparedCommand | None:
        self.invalidate()
        revision = self.revision
        result = await self.execute(command)
        if self.active and revision == self.revision:
            self.prepared = result
            return result
        return None

    async def submit(self) -> Any:
        if self.prepared is None:
            raise TraderError("REVIEW_REQUIRED", "review the current draft before submitting")
        prepared = self.prepared  # Preserve the idempotency key for repeated clicks.
        session = self.runtime.session
        assert session is not None
        return await self.execute(lambda: submit_prepared(session, prepared))
