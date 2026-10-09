"""Detached native results and opaque, scope-bound keyset cursors."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..domain import Budget
from ..storage import (
    AccountingObservation,
    AuditEvent,
    Currency,
    ExecutionState,
    Intent,
    Lifecycle,
    Operation,
    Order,
    Position,
    PositionState,
    Side,
    Trader,
)


class Period(StrEnum):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"


class Resolution(StrEnum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class Cursor:
    """Opaque keyset position; reuse only with the original session and filters."""

    _scope: object
    _query: tuple[object, ...]
    _after: int
    _ceiling: int


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: tuple[T, ...]
    next_cursor: Cursor | None


@dataclass(frozen=True, slots=True)
class RefreshStatus:
    running: bool
    refreshing: bool
    queued: bool
    last_attempt: datetime | None
    last_success: datetime | None
    next_attempt: datetime | None
    error: str | None
    generation: int
    stale: bool


@dataclass(frozen=True, slots=True)
class Valuation:
    observed_at: datetime | None
    equity: Decimal | None
    cash: Decimal | None
    unrealized_pnl: Decimal | None
    exposure: Decimal | None
    reasons: tuple[str, ...]
    realized_pnl: Decimal | None = None
    total_pnl: Decimal | None = None
    price_stale: bool = False


@dataclass(frozen=True, slots=True)
class Dashboard:
    # Memo handle for the selected local trader, not a UUID link.
    trader: Trader = field(repr=False)
    lifecycle: Lifecycle
    initialized: bool
    currency: Currency
    strategy_budget: Budget
    owner_budget: Budget
    owner_allocation: Decimal
    valuation: Valuation
    operation_counts: dict[str, int]
    refresh: RefreshStatus
    trading_mode: str = "bound"
    direct_account_verified: bool = False


@dataclass(frozen=True, slots=True)
class Statistics:
    period: Period
    start: datetime
    collection_started_at: datetime
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    equity: Decimal | None
    cash: Decimal | None
    unrealized_pnl: Decimal | None
    exposure: Decimal | None
    confirmed_realized_pnl: Decimal | None
    confirmed_costs: Decimal | None
    absolute_equity_change: Decimal | None
    sampled_equity_peak: Decimal | None
    maximum_drawdown: Decimal | None
    sample_count: int
    missing_intervals: int
    reasons: tuple[str, ...]
    operation_counts: dict[str, int]
    generation: int
    stale: bool


@dataclass(frozen=True, slots=True)
class OperationView:
    # Persistent Intent entity handle accepted by get_operation and intent filters.
    intent: Intent = field(repr=False)
    sequence: int
    created_at: datetime
    operation: Operation
    state: ExecutionState
    params: dict[str, Any]
    error_code: str | None
    # Broker's external numeric order/position identifiers, absent until reported.
    broker_order_id: str | None
    broker_position_id: str | None


@dataclass(frozen=True, slots=True)
class PositionView:
    # Persistent local projection and its originating operation, used only as handles.
    position: Position = field(repr=False)
    intent: Intent = field(repr=False)
    # External broker numeric position ID represented as text.
    position_id: str
    sequence: int
    created_at: datetime
    symbol: str
    state: PositionState
    side: Side
    # External broker instrument catalogue ID.
    instrument_id: int
    leverage: int
    strategy_notional_usd: Decimal
    units: Decimal
    stop_loss_rate: Decimal | None
    take_profit_rate: Decimal | None
    entry_price: Decimal | None = None
    liquidation_price: Decimal | None = None
    remaining_units: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    price_refreshed_at: datetime | None = None
    price_stale: bool = False


@dataclass(frozen=True, slots=True)
class OrderView:
    # Persistent local projection and its originating operation, used only as handles.
    order: Order = field(repr=False)
    intent: Intent = field(repr=False)
    # External broker numeric order ID represented as text.
    order_id: str
    sequence: int
    created_at: datetime
    symbol: str
    state: ExecutionState
    operation: Operation | None = None


@dataclass(frozen=True, slots=True)
class AuditView:
    # Persistent immutable event and associated operation handles.
    event: AuditEvent = field(repr=False)
    intent: Intent | None = field(repr=False)
    sequence: int
    occurred_at: datetime
    kind: str
    actor: str
    source: str
    facts: dict[str, Any]
    previous_hash: str
    event_hash: str


@dataclass(frozen=True, slots=True)
class AccountingView:
    # Immutable accounting observation and associated operation handles.
    observation: AccountingObservation = field(repr=False)
    intent: Intent = field(repr=False)
    sequence: int
    occurred_at: datetime
    kind: str
    amount: Decimal | None
    delta: Decimal | None
    confirmed: bool


@dataclass(frozen=True, slots=True)
class OperationDetail:
    operation: OperationView
    orders: Page[OrderView]
    positions: Page[PositionView]
    accounting: Page[AccountingView]


@dataclass(frozen=True, slots=True)
class ChartPoint:
    start: datetime
    first_equity: Decimal | None
    last_equity: Decimal | None
    min_equity: Decimal | None
    max_equity: Decimal | None
    cash: Decimal | None
    unrealized_pnl: Decimal | None
    exposure: Decimal | None
    sample_count: int
    first_observed_at: datetime | None
    last_observed_at: datetime | None
    missing_intervals: int
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortfolioChart:
    start: datetime
    end: datetime
    resolution: Resolution
    buckets: tuple[ChartPoint, ...]
    generation: int
    stale: bool
