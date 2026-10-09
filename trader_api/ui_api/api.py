"""Native, prefix-scoped local UI API.

Read functions return detached values from the latest committed generation, never
perform network I/O or rebuild history. Entity references are handles only. All
time ranges are aware UTC, start-inclusive/end-exclusive. List functions use
ascending immutable creation sequence, default 100 and maximum 1,000 rows. A
cursor freezes the append ceiling; state filters are evaluated at each page read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from datetime import datetime, timedelta
from itertools import islice
from typing import TYPE_CHECKING, Any

import dbzero as db0  # type: ignore[import-untyped]

from ..domain import Budget, utc_now
from ..errors import TraderError
from ..portfolio import position_value, totals
from ..serialization import native_copy
from ..storage import (
    AccountingObservation,
    AuditEvent,
    ChartBucket,
    ExecutionState,
    Intent,
    Operation,
    Order,
    PeriodSummary,
    PortfolioBinding,
    Position,
    PositionState,
    RefreshState,
    TraderState,
    _runtime_lock,
)
from .queries import page, utc
from .refresh import RefreshWorker
from .session import Session, check_handle, selected
from .types import (
    AccountingView,
    AuditView,
    ChartPoint,
    Cursor,
    Dashboard,
    OperationDetail,
    OperationView,
    OrderView,
    Page,
    Period,
    PortfolioChart,
    PositionView,
    RefreshStatus,
    Resolution,
    Statistics,
    Valuation,
)
from .updates import SECONDS, aggregate_key, bucket_start, current, period_start, periods

if TYPE_CHECKING:
    from ..service import TraderService


def open_session(service: TraderService, *, clock: Callable[[], datetime] = utc_now) -> Session:
    """Capture the already selected trader prefix from an existing verified service.

    `service` supplies the existing broker and verified credential/environment
    binding; this API introduces no UI login. `clock` supplies aware times (UTC
    normalized), including deterministic clocks for tests. Returns a Session for
    this prefix only. Raises TraderError for absent/mismatched trader, binding,
    read scope or selected prefix. Initializes empty persisted collection state
    and current periods atomically, with no historical backfill or network work.
    It does not start a thread. Use start_refresh/stop_refresh for app lifecycle.
    """
    with _runtime_lock:
        service._require_context()
        assert service.store is not None and service.prefix is not None
        assert service.trader is not None and service._context is not None
        captured = "/" + db0.get_current_prefix().name.lstrip("/")
        if captured != service.prefix:
            raise TraderError("TRADER_MISMATCH", "select the service trader prefix before opening a UI session")
        state = service.store.one(TraderState, prefix=captured)
        binding = service.store.one(PortfolioBinding, prefix=captured)
        context = service._context
        if service.trading_mode == "standalone":
            service._validate_binding()
            if state is None or not context.can_read:
                raise TraderError("DIRECT_ACCOUNT_IDENTITY_UNVERIFIED", "direct account read is unavailable")
        elif (
            state is None
            or binding is None
            or not binding.binding_version
            or state.trader != service.trader
            or binding.trader != service.trader
            or binding.environment != state.environment
            or str(state.environment).lower() != context.environment.value
            or binding.agent_trading_account_id != context.trading_account_id
            or binding.agent_trading_portfolio_id != context.trading_portfolio_id
            or binding.credential_fingerprint != context.credential_fingerprint
            or not context.can_read
        ):
            raise TraderError("PORTFOLIO_SCOPE_MISMATCH", "selected trader has no matching verified read binding")
        session = Session(service.store, captured, service.trader, context, service, clock)
        object.__setattr__(session, "_worker", RefreshWorker(session))
        with selected(session, write=True):
            now = utc(clock())
            periods(current(now), now)
            RefreshState()
        return session


def _refresh_status(session: Session) -> RefreshStatus:
    state = RefreshState()
    summary = current(utc(session._clock()))
    worker = session._worker
    assert worker is not None
    with worker.condition:
        running, refreshing, queued = worker.running, worker.refreshing, worker.queued
    stale = state.last_success is None or (utc(session._clock()) - state.last_success).total_seconds() > 90
    return RefreshStatus(
        running,
        refreshing,
        queued,
        state.last_attempt,
        state.last_success,
        state.next_attempt if running else None,
        "REFRESH_INTERRUPTED" if state.in_progress and not running else state.error,
        summary.generation,
        stale,
    )


def get_refresh_status(session: Session) -> RefreshStatus:
    """Return scheduler state, sanitized error, generation and 90-second freshness.

    `session` fixes the trader scope. This local read has no side effects or
    ordering; no successful collection means stale. Raises SESSION_CLOSED if the
    database is closed. Interrupted persisted attempts never imply success.
    """
    with selected(session):
        return _refresh_status(session)


def start_refresh(session: Session) -> None:
    """Start this session's background collector immediately, then every 60 seconds.

    Idempotent while running. Returns None; performs short database writes and
    starts one observational network worker. Persisted failure deadlines survive
    restart. Raises SESSION_CLOSED or REFRESH_RUNNING if another session owns the
    same prefix. Results become visible only after an atomic successful update.
    """
    assert session._worker is not None
    session._worker.start()


def stop_refresh(session: Session) -> None:
    """Stop scheduling and join this session's in-flight collection; return None.

    Idempotent. A collection already running may commit before return; no further
    calls start. Call before closing dbzero. Retains last-good observations and
    failure backoff. Raises SESSION_CLOSED if the database was closed prematurely.
    """
    assert session._worker is not None
    session._worker.stop()


def request_refresh(session: Session) -> RefreshStatus:
    """Queue one observational update and return current status without waiting.

    Uses only the captured scope. Repeated requests coalesce; an in-flight read
    satisfies requests received during it. Before start_refresh this only queues
    work. Failure/rate-limit deadlines take precedence over manual requests.
    Raises SESSION_CLOSED for a closed database; never places or reconciles trades.
    """
    with selected(session):
        assert session._worker is not None
        session._worker.request()
        return _refresh_status(session)


def get_dashboard(session: Session) -> Dashboard:
    """Return this trader's current lifecycle, budgets, valuation, counts and freshness.

    Valuation uses only confirmed local fills, costs, and cached liquidation
    prices. Legacy account-wide snapshots are excluded. Values are detached under
    the database lock, without network calls. Missing required data remains None.
    `session` fixes scope; a closed database raises SESSION_CLOSED.
    """
    with selected(session):
        state = session._store.state(session._prefix, session.trader)
        binding = session._store.binding(session._prefix)
        standalone = session._service.trading_mode == "standalone"
        summary = current(utc(session._clock()))
        realized, unrealized, price_at = totals(state.initialized)
        total = None if realized is None or unrealized is None else realized + unrealized
        return Dashboard(
            session.trader,
            session._service._direct().lifecycle if standalone else binding.lifecycle,
            state.initialized,
            state.currency,
            Budget(state.strategy_initial_cap, state.strategy_realized, state.strategy_committed),
            Budget(state.owner_initial_cap, state.owner_realized, state.owner_committed),
            state.owner_initial_cap if standalone else binding.investment_usd,
            Valuation(
                price_at,
                None if total is None else state.strategy_initial_cap + total,
                None,
                unrealized,
                None,
                ("waiting_for_confirmed_data",) if total is None else (),
                realized,
                total,
                price_at is not None and (session._clock() - price_at).total_seconds() > 90,
            ),
            dict(summary.operation_counts),
            _refresh_status(session),
            session._service.trading_mode,
            standalone,
        )


def get_statistics(session: Session, period: Period = Period.TODAY) -> Statistics:
    """Read one persisted UTC period (today, Monday week, month, or collection lifetime).

    `session` fixes scope; `period` selects a direct period/start lookup. Returns
    detached Statistics, with observed equity change and sampled absolute peak
    drawdown (not cash-flow-adjusted investment returns). No source rows are read
    or recalculated. An unprocessed rollover returns `period_not_collected`, and
    does not create/reset summaries. Missing financial totals remain None.
    Raises ValueError for an invalid period and SESSION_CLOSED for a closed DB.
    """
    period = Period(period)
    with selected(session):
        summary = current(utc(session._clock()))
        start = period_start(period, utc(session._clock()), summary.started_at)
        item = next(iter(db0.find(PeriodSummary, aggregate_key(period, start), prefix=session._prefix)), None)
        refresh = _refresh_status(session)
        if item is None:
            return Statistics(
                period,
                start,
                summary.started_at,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                0,
                0,
                ("period_not_collected",),
                {},
                summary.generation,
                refresh.stale,
            )
        change = None if item.first_equity is None or item.last_equity is None else item.last_equity - item.first_equity
        reasons = list(item.reasons)
        if item.sample_count == 0:
            reasons.append("valuation_not_collected")
        if item.costs is None:
            reasons.append("confirmed_costs_unavailable")
        if item.realized_pnl is None:
            reasons.append("confirmed_realized_pnl_unavailable")
        if item.unconfirmed_costs:
            reasons.append("incomplete_confirmed_costs")
        if item.unconfirmed_realized_pnl:
            reasons.append("incomplete_confirmed_realized_pnl")
        if refresh.stale:
            reasons.append("stale")
        return Statistics(
            period,
            start,
            summary.started_at,
            item.first_observed_at,
            item.last_observed_at,
            item.last_equity,
            item.cash,
            item.unrealized_pnl,
            item.exposure,
            item.realized_pnl,
            item.costs,
            change,
            item.max_equity,
            item.max_drawdown,
            item.sample_count,
            item.missing_intervals,
            tuple(reasons),
            dict(item.operation_counts),
            summary.generation,
            refresh.stale,
        )


def _operation(item: Intent) -> OperationView:
    return OperationView(
        item,
        item.sequence,
        item.created_at,
        item.operation,
        item.state,
        native_copy(item.params),
        item.error_code,
        item.broker_order_id,
        item.broker_position_id,
    )


def _position(item: Position, now: datetime | None = None) -> PositionView:
    value = position_value(item)
    return PositionView(
        item,
        **{f.name: native_copy(getattr(item, f.name)) for f in fields(PositionView)[1:14]},
        entry_price=value.entry_price,
        liquidation_price=value.liquidation_price,
        remaining_units=value.remaining_units,
        unrealized_pnl=value.unrealized_pnl,
        price_refreshed_at=value.price_refreshed_at,
        price_stale=value.price_refreshed_at is not None
        and ((now or utc_now()) - value.price_refreshed_at).total_seconds() > 90,
    )


def _order(item: Order) -> OrderView:
    return OrderView(
        item,
        **{f.name: native_copy(getattr(item, f.name)) for f in fields(OrderView)[1:-1]},
        operation=item.intent.operation,
    )


def _audit(item: AuditEvent) -> AuditView:
    return AuditView(item, **{f.name: native_copy(getattr(item, f.name)) for f in fields(AuditView)[1:]})


def _accounting(item: AccountingObservation) -> AccountingView:
    return AccountingView(item, **{f.name: native_copy(getattr(item, f.name)) for f in fields(AccountingView)[1:]})


def list_operations(
    session: Session,
    *,
    operation: Operation | None = None,
    state: ExecutionState | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 100,
    cursor: Cursor | None = None,
) -> Page[OperationView]:
    """Return local operations filtered by operation/state and creation time [start,end).

    `session` fixes scope; omitted filters match all. `limit` is 1..1000, default
    100. `cursor` continues ascending creation-sequence order at its frozen append
    ceiling. Filters see current committed state, so later state changes can alter
    membership. No network/writes. Raises INVALID_PAGE_SIZE, INVALID_RANGE,
    INVALID_CURSOR or SESSION_CLOSED. Values are detached at read time.
    """
    with selected(session):
        return page(
            session,
            Intent,
            _operation,
            tags=tuple(x for x in (operation, state) if x is not None),
            start=start,
            end=end,
            limit=limit,
            cursor=cursor,
        )


def _intent_tags(session: Session, intent: Intent | None, *tags: object | None) -> tuple[object, ...]:
    result = tuple(x for x in tags if x is not None)
    if intent is not None:
        check_handle(session, intent, Intent)
        result += (db0.as_tag(intent),)
    return result


def list_positions(
    session: Session,
    *,
    state: PositionState | None = None,
    symbol: str | None = None,
    # Persistent local Intent handle, not a broker ID or UUID string.
    intent: Intent | None = None,
    limit: int = 100,
    cursor: Cursor | None = None,
) -> Page[PositionView]:
    """Return indexed local positions for state/symbol/intent, ascending creation sequence.

    `session` fixes scope; omitted filters match all. `limit` is 1..1000 (default
    100); `cursor` freezes the append ceiling, with state membership evaluated per
    read. Returns detached committed projections, not a live broker refresh.
    No side effects. Raises INVALID_HANDLE/TRADER_MISMATCH for foreign intent,
    INVALID_PAGE_SIZE/INVALID_CURSOR, or SESSION_CLOSED.
    """
    with selected(session):
        return page(
            session,
            Position,
            lambda item: _position(item, session._clock()),
            tags=_intent_tags(session, intent, state, None if symbol is None else f"symbol:{symbol}"),
            limit=limit,
            cursor=cursor,
        )


def list_orders(
    session: Session,
    *,
    state: ExecutionState | None = None,
    symbol: str | None = None,
    # Persistent local Intent handle, not a broker ID or UUID string.
    intent: Intent | None = None,
    limit: int = 100,
    cursor: Cursor | None = None,
) -> Page[OrderView]:
    """Return indexed local orders for state/symbol/intent, ascending creation sequence.

    `session` fixes scope; omitted filters match all. `limit` is 1..1000 (default
    100); `cursor` freezes the append ceiling, with state membership evaluated per
    read. Returns detached committed projections, without network or writes.
    Raises INVALID_HANDLE/TRADER_MISMATCH for foreign intent,
    INVALID_PAGE_SIZE/INVALID_CURSOR, or SESSION_CLOSED.
    """
    with selected(session):
        return page(
            session,
            Order,
            _order,
            tags=_intent_tags(session, intent, state, None if symbol is None else f"symbol:{symbol}"),
            limit=limit,
            cursor=cursor,
        )


def list_audit_events(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    kind: str | None = None,
    actor: str | None = None,
    source: str | None = None,
    # Persistent local Intent handle associated with the audit record.
    intent: Intent | None = None,
    limit: int = 100,
    cursor: Cursor | None = None,
) -> Page[AuditView]:
    """Return immutable audit records by time [start,end), kind, actor, source and intent.

    `session` fixes scope; omitted filters match all. Ordered by audit sequence.
    `limit` is 1..1000 (default 100); `cursor` freezes the append ceiling. Returned
    facts are detached; hashes are unchanged. Reads only local committed data.
    No writes/network. Raises INVALID_RANGE, INVALID_PAGE_SIZE, INVALID_CURSOR,
    INVALID_HANDLE/TRADER_MISMATCH for foreign intent, or SESSION_CLOSED.
    """
    with selected(session):
        tags = tuple(
            f"{key}:{value}"
            for key, value in (("kind", kind), ("actor", actor), ("source", source))
            if value is not None
        )
        return page(
            session,
            AuditEvent,
            _audit,
            tags=_intent_tags(session, intent, *tags),
            start=start,
            end=end,
            time_field="occurred_at",
            limit=limit,
            cursor=cursor,
        )


def list_accounting_observations(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    kind: str | None = None,
    # Persistent local Intent handle for the reported accounting contribution.
    intent: Intent | None = None,
    limit: int = 100,
    cursor: Cursor | None = None,
) -> Page[AccountingView]:
    """Return estimates, confirmed contributions and transitions by ascending sequence.

    `session` fixes scope; kind/intent/time [start,end) filters are optional.
    `limit` is 1..1000 (default 100); `cursor` freezes the append ceiling. Amount is
    the reported per-intent total; delta is the increment applied once. Estimates
    never enter confirmed statistics. Detached committed values; no side effects.
    Raises INVALID_RANGE/PAGE_SIZE/CURSOR, INVALID_HANDLE/TRADER_MISMATCH or SESSION_CLOSED.
    """
    with selected(session):
        return page(
            session,
            AccountingObservation,
            _accounting,
            tags=_intent_tags(session, intent, kind),
            start=start,
            end=end,
            time_field="occurred_at",
            limit=limit,
            cursor=cursor,
        )


def get_operation(
    session: Session,
    # Persistent local Intent handle obtained from list_operations, never a UUID string.
    intent: Intent,
) -> OperationDetail:
    """Return one operation and first pages of related orders, positions and accounting.

    `session` fixes scope; `intent` must belong to it. Each related page contains
    at most 100 records, ascending sequence; continue through the corresponding
    list function with the same intent filter and returned cursor. Returns
    detached committed values with no network or writes. Raises INVALID_HANDLE,
    TRADER_MISMATCH or SESSION_CLOSED. No historical aggregate computation occurs.
    """
    with selected(session):
        check_handle(session, intent, Intent)
        return OperationDetail(
            _operation(intent),
            list_orders(session, intent=intent),
            list_positions(session, intent=intent),
            list_accounting_observations(session, intent=intent),
        )


def get_portfolio_chart(
    session: Session,
    start: datetime,
    end: datetime,
    *,
    resolution: Resolution | None = None,
) -> PortfolioChart:
    """Read at most 1,000 stored valuation buckets intersecting UTC [start,end).

    `session` fixes scope. `resolution` selects minute/hour/day; None picks the
    finest resolution whose aligned range fits 1,000 buckets. Boundary buckets
    include their whole stored interval. Empty buckets stay gaps. Returns detached
    buckets ordered by start and the last committed generation/freshness; no
    regrouping, source scans, network or writes. Raises INVALID_RANGE for naive,
    empty or >1,000-bucket ranges (even daily), ValueError for invalid resolution,
    or SESSION_CLOSED. Equity is strategy equity only.
    """
    start, end = utc(start), utc(end)
    if start >= end:
        raise TraderError("INVALID_RANGE", "start must precede end")
    choices = tuple(Resolution) if resolution is None else (Resolution(resolution),)
    chosen = None
    for candidate in choices:
        first = bucket_start(candidate, start)
        last = bucket_start(candidate, end - timedelta(milliseconds=1))
        if int((last - first).total_seconds()) // SECONDS[candidate] + 1 <= 1000:
            chosen = candidate
            break
    if chosen is None:
        raise TraderError("INVALID_RANGE", "chart range exceeds 1000 buckets at the requested resolution")
    with selected(session):
        index = db0.index_of(ChartBucket, "start", prefix=session._prefix)
        query = db0.find(ChartBucket, str(chosen), index.select(first, last), prefix=session._prefix)
        rows = tuple(islice(index.sort(query), 1001))
        if len(rows) > 1000:
            raise TraderError("INVALID_RANGE", "chart has more than 1000 stored buckets")
        points = tuple(ChartPoint(**{f.name: _chart_value(item, f.name) for f in fields(ChartPoint)}) for item in rows)
        refresh = _refresh_status(session)
        return PortfolioChart(start, end, chosen, points, refresh.generation, refresh.stale)


def _chart_value(item: Any, name: str) -> Any:
    value = getattr(item, name)
    return tuple(value) if name == "reasons" else value
