from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4


@dataclass(slots=True)
class WorkspaceState:
    """Server-held state for one browser tab/client connection."""

    identity: str = field(default_factory=lambda: uuid4().hex)
    route_epoch: int = 0
    selected_key: str | None = None

    def select(self, key: str) -> None:
        self.route_epoch += 1
        self.selected_key = key
