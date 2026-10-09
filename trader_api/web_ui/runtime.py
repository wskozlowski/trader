"""Process-wide shared trader runtimes."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum

from ..errors import TraderError
from ..service import TraderService
from ..storage import _runtime_lock
from ..ui_api import Session, open_session, start_refresh, stop_refresh
from .catalog import Catalog, CatalogEntry

logger = logging.getLogger(__name__)


class RuntimeState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED_STARTUP = "FAILED_STARTUP"


@dataclass(slots=True)
class TraderRuntime:
    entry: CatalogEntry
    state: RuntimeState = RuntimeState.NOT_STARTED
    service: TraderService | None = field(default=None, repr=False)
    session: Session | None = field(default=None, repr=False)
    error_code: str | None = None
    command_executor: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))
    broker_gate: threading.RLock = field(default_factory=threading.RLock, repr=False)
    accepting_commands: bool = False

    def start(self, factory: Callable[[CatalogEntry], TraderService], environment: str) -> None:
        if self.state == RuntimeState.RUNNING:
            return
        self.state = RuntimeState.STARTING
        try:
            service = factory(self.entry)
            if service.trading_mode != self.entry.trading_mode:
                raise TraderError("TRADING_MODE_MISMATCH", "catalog and credential profile modes differ")
            service._require_context()
            if service.trading_mode == "standalone" and not service.capabilities()["direct_account_verified"]:
                raise TraderError("DIRECT_ACCOUNT_IDENTITY_UNVERIFIED", "direct broker account is not ready")
            with _runtime_lock:
                assert service.store is not None and service.prefix is not None
                service.store.open(service.prefix)
                session = open_session(service)
            start_refresh(session)
            self.service, self.session = service, session
            self.accepting_commands = True
            self.state = RuntimeState.RUNNING
        except TraderError as exc:
            logger.exception("Trader %s initialization failed [%s]", self.entry.key, exc.code)
            self.error_code = exc.code
            self.state = RuntimeState.FAILED_STARTUP
            self.accepting_commands = False
        except Exception:
            logger.exception("Trader %s initialization failed", self.entry.key)
            self.error_code = "STARTUP_FAILED"
            self.state = RuntimeState.FAILED_STARTUP
            self.accepting_commands = False

    def stop(self) -> None:
        if self.state in {RuntimeState.STOPPED, RuntimeState.NOT_STARTED}:
            self.state = RuntimeState.STOPPED
            return
        self.state = RuntimeState.STOPPING
        self.accepting_commands = False
        if self.session is not None:
            stop_refresh(self.session)
        self.command_executor.shutdown(wait=True)
        self.state = RuntimeState.STOPPED


class RuntimeRegistry:
    def __init__(
        self,
        catalog: Catalog,
        *,
        environment: str,
        service_factory: Callable[[CatalogEntry], TraderService] | None = None,
    ) -> None:
        self.catalog = catalog
        self.environment = environment
        self._factory = service_factory or (
            lambda entry: TraderService(
                entry.trader_name,
                entry.profile_filename,
                expected_environment=environment,
            )
        )
        self._runtimes: dict[str, TraderRuntime] = {}
        self._lock = threading.RLock()
        self._stopping = False

    def open(self, key: str) -> TraderRuntime:
        with self._lock:
            if self._stopping:
                raise TraderError("STOPPING", "application is stopping")
            entry = self.catalog.by_key(key)
            runtime = self._runtimes.setdefault(key, TraderRuntime(entry))
            if runtime.state == RuntimeState.NOT_STARTED:
                runtime.start(self._factory, self.environment)
            return runtime

    def get(self, key: str) -> TraderRuntime | None:
        with self._lock:
            return self._runtimes.get(key)

    def shutdown(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            runtimes = tuple(self._runtimes.values())
        for runtime in runtimes:
            runtime.stop()
