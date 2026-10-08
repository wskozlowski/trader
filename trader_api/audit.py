from __future__ import annotations

from typing import Any

from .storage import DbzeroStore, TraderStateMemo


def verify_chain(store: DbzeroStore, prefix: str, state: TraderStateMemo) -> dict[str, Any]:
    return store.verify_audit(prefix, state)
