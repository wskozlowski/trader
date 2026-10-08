from __future__ import annotations

from typing import Any

from .service import TraderService


def submit_preview(service: TraderService, preview_id: str, idempotency_key: str) -> dict[str, Any]:
    return service.submit(preview_id, idempotency_key)
