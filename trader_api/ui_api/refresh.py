"""Explicit observational refresh lifecycle; importing this module starts no threads."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal

import dbzero as db0  # type: ignore[import-untyped]

from ..auth import resolve_context
from ..broker.observations import PriceQuote
from ..domain import ScopeEvidence, VerifiedContext
from ..errors import TraderError
from ..serialization import storage_datetime
from ..storage import ExecutionState, Intent, PortfolioBinding, Position, PositionPrice, PositionState, RefreshState
from .session import Session, selected
from .updates import current, periods


class _StoredVerifier:
    def __init__(self, session: Session) -> None:
        self.session = session

    def verify(self, *, api_key: str, user_key: str) -> ScopeEvidence | None:
        record = self.session._store.scope_evidence(self.session._context.credential_fingerprint)
        if record is None:
            return None
        return ScopeEvidence(
            frozenset(record.scopes),
            record.subject_id,
            record.trading_account_id,
            record.trading_portfolio_id,
            record.issued_at,
            record.expires_at,
            record.revoked,
            record.source,
        )


def validate_scope(session: Session, now: datetime) -> VerifiedContext:
    if session._service.trading_mode == "standalone":
        session._service._validate_binding()
        return session._context
    context = resolve_context(
        session._service.profile,
        _StoredVerifier(session),
        expected_environment=session._context.environment,
        now=now,
    )
    session._store.open(session._prefix)
    binding = PortfolioBinding()
    if (
        binding.trader != session.trader
        or context.trading_account_id != session._context.trading_account_id
        or context.trading_portfolio_id != session._context.trading_portfolio_id
        or binding.credential_fingerprint != context.credential_fingerprint
        or binding.agent_trading_account_id != context.trading_account_id
        or binding.agent_trading_portfolio_id != context.trading_portfolio_id
        or str(binding.environment).lower() != context.environment.value
        or not context.can_read
    ):
        raise TraderError("PORTFOLIO_SCOPE_MISMATCH", "portfolio read binding is no longer valid")
    return context


_registry_lock = threading.Lock()
_active: dict[str, RefreshWorker] = {}


class RefreshWorker:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.condition = threading.Condition()
        self.thread: threading.Thread | None = None
        self.running = False
        self.refreshing = False
        self.queued = False
        self.due: datetime | None = None
        self.retry_not_before: datetime | None = None

    def start(self) -> None:
        with self.session._lifecycle_lock, _registry_lock:
            if self.running:
                return
            existing = _active.get(self.session._prefix)
            if existing is not None and existing is not self:
                raise TraderError("REFRESH_RUNNING", "another session is refreshing this trader")
            with selected(self.session, write=True):
                state = RefreshState()
                now = storage_datetime(self.session._clock())
                # Preserve a broker rate-limit/backoff deadline over restarts.
                self.retry_not_before = state.next_attempt if state.failures else None
                self.due = max(now, self.retry_not_before or now)
                if state.in_progress:
                    state.error = "REFRESH_INTERRUPTED"
                state.in_progress = False
                state.next_attempt = self.due
            with self.condition:
                self.running = True
                self.queued = True
            _active[self.session._prefix] = self
            self.thread = threading.Thread(target=self._run, name="trader-ui-refresh", daemon=True)
            self.thread.start()

    def request(self, *, execution_changed: bool = False) -> None:
        with self.condition:
            # Requests during one collection are satisfied by that collection.
            if not self.refreshing or execution_changed:
                self.queued = True
            self.condition.notify_all()

    def stop(self) -> None:
        with self.session._lifecycle_lock:
            with self.condition:
                self.running = False
                self.queued = False
                self.condition.notify_all()
            if self.thread is not None and self.thread is not threading.current_thread():
                self.thread.join()
            with _registry_lock:
                if _active.get(self.session._prefix) is self:
                    del _active[self.session._prefix]
            with selected(self.session, write=True):
                state = RefreshState()
                state.in_progress = False
                if not state.failures:
                    state.next_attempt = None

    def tick(self) -> bool:
        """One scheduler step, also usable with an injected fake clock in tests."""
        now = storage_datetime(self.session._clock())
        with self.condition:
            if not self.running or self.refreshing:
                return False
            if self.retry_not_before is not None and now < self.retry_not_before:
                return False
            if not self.queued and self.due is not None and now < self.due:
                return False
            self.queued = False
            self.refreshing = True
        try:
            self._collect(now)
        finally:
            with self.condition:
                self.refreshing = False
                self.condition.notify_all()
        return True

    def _collect(self, now: datetime) -> None:
        session = self.session
        try:
            with selected(session, write=True):
                state = RefreshState()
                state.last_attempt = now
                state.in_progress = True
                periods(current(now), now)
            with selected(session):
                context = validate_scope(session, now)
            with selected(session):
                initialized = session._store.state(session._prefix, session.trader).initialized
                pending = any(
                    i.state in {ExecutionState.ADMITTED, ExecutionState.UNKNOWN, ExecutionState.ACKNOWLEDGED}
                    for i in db0.find(Intent)
                )
            errors: list[Exception] = []
            if initialized:
                try:
                    if pending:
                        session._service.reconcile()
                    session._service.refresh_execution_details()
                except Exception as exc:
                    errors.append(exc)
            with selected(session):
                positions = [(p, p.instrument_id) for p in db0.find(Position, PositionState.OPEN)]
            reader = getattr(session._service.broker, "quote", None)
            quotes: dict[int, PriceQuote] = {}
            for instrument in {instrument for _, instrument in positions}:
                try:
                    if reader is None:
                        raise TraderError("VALUATION_UNAVAILABLE", "broker has no price reader")
                    quote = reader(context, instrument, storage_datetime(session._clock()))
                    finished = storage_datetime(session._clock())
                    if (
                        not isinstance(quote, PriceQuote)
                        or quote.instrument_id != instrument
                        or quote.refreshed_at > finished
                        or quote.refreshed_at < now - timedelta(seconds=90)
                        or any(not isinstance(v, Decimal) or not v.is_finite() for v in (quote.bid, quote.ask))
                        or quote.bid <= 0
                        or quote.ask < quote.bid
                    ):
                        raise TraderError("VALUATION_UNAVAILABLE", "broker returned an invalid quote")
                    quotes[instrument] = quote
                except Exception as exc:
                    errors.append(exc)
            finished = storage_datetime(session._clock())
            with selected(session, write=True):
                validate_scope(session, finished)
                for position, instrument in positions:
                    quote = quotes.get(instrument)
                    if quote is None or position.state != PositionState.OPEN:
                        continue
                    cached = next(iter(db0.find(PositionPrice, db0.as_tag(position))), None)
                    if cached is None:
                        cached = PositionPrice(position, quote.bid, quote.ask, storage_datetime(quote.refreshed_at))
                        db0.tags(cached).add("POSITION_PRICE")
                    elif quote.refreshed_at >= cached.refreshed_at:
                        cached.bid, cached.ask = quote.bid, quote.ask
                        cached.refreshed_at = storage_datetime(quote.refreshed_at)
                current(finished).generation += 1
            if errors:
                raise errors[0]
            with selected(session, write=True):
                state = RefreshState()
                state.last_success = finished
                state.in_progress = False
                state.error = None
                state.failures = 0
                self.due = finished + timedelta(seconds=60)
                state.next_attempt = self.due
                self.retry_not_before = None
        except Exception as exc:
            logging.getLogger(__name__).exception("Portfolio refresh failed for trader %s", session._service.trader_id)
            finished = storage_datetime(session._clock())
            with selected(session, write=True):
                state = RefreshState()
                state.in_progress = False
                state.failures += 1
                # Never persist arbitrary broker/exception text or response bodies.
                allowed = {
                    "BROKER_RATE_LIMITED",
                    "BROKER_UNAVAILABLE",
                    "BROKER_OUTCOME_UNKNOWN",
                    "VALUATION_UNAVAILABLE",
                    "ACCOUNT_MISMATCH",
                    "PERMISSION_REVOKED",
                    "PORTFOLIO_SCOPE_MISMATCH",
                    "ENVIRONMENT_UNVERIFIED",
                    "CONFIG_INVALID",
                }
                state.error = exc.code if isinstance(exc, TraderError) and exc.code in allowed else "REFRESH_FAILED"
                delay = min(300, 60 * 2 ** min(state.failures - 1, 3))
                if isinstance(exc, TraderError):
                    retry_after = exc.details.get("retry_after_seconds")
                    if isinstance(retry_after, (int, float)) and retry_after > delay:
                        delay = retry_after
                self.due = finished + timedelta(seconds=delay)
                self.retry_not_before = self.due
                state.next_attempt = self.due

    def _run(self) -> None:
        try:
            while True:
                with self.condition:
                    if not self.running:
                        return
                self.tick()
                with self.condition:
                    if not self.running:
                        return
                    now = self.session._clock()
                    deadline = self.retry_not_before or self.due or now
                    timeout = max(0.01, (deadline - now).total_seconds())
                    if self.queued and self.retry_not_before is None:
                        timeout = 0.01
                    self.condition.wait(timeout=min(timeout, 60))
        finally:
            with self.condition:
                self.running = False
                self.refreshing = False
            with _registry_lock:
                if _active.get(self.session._prefix) is self:
                    del _active[self.session._prefix]
