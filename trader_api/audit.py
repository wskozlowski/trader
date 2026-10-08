from __future__ import annotations

from typing import Any

from .storage import DbzeroStore, TraderState


def verify_chain(store: DbzeroStore, prefix: str, state: TraderState) -> dict[str, Any]:
    return store.verify_audit(prefix, state)
