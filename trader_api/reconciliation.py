from __future__ import annotations

from typing import Any

from .service import TraderService


def reconcile_trader(service: TraderService) -> dict[str, Any]:
    """Explicit entry point used by coordinator workers and operational tooling."""
    return service.reconcile()
