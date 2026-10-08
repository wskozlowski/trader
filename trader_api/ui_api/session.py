"""Captured local scope and shared database serialization."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import dbzero as db0  # type: ignore[import-untyped]

from ..domain import VerifiedContext
from ..errors import TraderError
from ..storage import DbzeroStore, Trader, _runtime_lock, current_dbzero_epoch, current_dbzero_root

if TYPE_CHECKING:
    from ..service import TraderService
    from .refresh import RefreshWorker


@dataclass(frozen=True, slots=True, eq=False)
class Session:
    """Application-local session. Construct with open_session; stop before closing dbzero."""

    _store: DbzeroStore
    _prefix: str
    # Memo handle identifying the one captured local trader.
    trader: Trader = field(repr=False)
    _context: VerifiedContext = field(repr=False)
    _service: TraderService = field(repr=False)
    _clock: Callable[[], datetime]
    _scope: object = field(default_factory=object)
    _epoch: object = field(default_factory=current_dbzero_epoch)
    _lifecycle_lock: threading.RLock = field(default_factory=threading.RLock)
    _worker: RefreshWorker | None = None


@contextmanager
def selected(session: Session, *, write: bool = False) -> Iterator[None]:
    with _runtime_lock:
        if current_dbzero_root() != session._store.root or current_dbzero_epoch() is not session._epoch:
            raise TraderError("SESSION_CLOSED", "the session database is no longer open")
        session._store.open(session._prefix)
        if write:
            with db0.atomic():
                yield
            session._store.commit(session._prefix)
        else:
            yield


def check_handle(session: Session, value: object, model: type[object]) -> None:
    if not isinstance(value, model):
        raise TraderError("INVALID_HANDLE", "expected a local memo entity handle")
    if db0.get_prefix_of(value).name != session._prefix.lstrip("/"):
        raise TraderError("TRADER_MISMATCH", "entity handle belongs to a different trader")
