"""Trader-local confirmed accounting. Call under the database lock and selected prefix."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

import dbzero as db0  # type: ignore[import-untyped]

from .domain import BrokerOutcome, money
from .storage import (
    ExecutionFill,
    ExecutionState,
    Intent,
    LocalAccountingVersion,
    Operation,
    OperationContribution,
    Position,
    PositionPrice,
    PositionState,
    TraderState,
)


@dataclass(frozen=True, slots=True)
class PositionValue:
    entry_price: Decimal | None
    liquidation_price: Decimal | None
    remaining_units: Decimal | None
    unrealized_pnl: Decimal | None
    price_refreshed_at: datetime | None


def fill_for(intent: Intent) -> ExecutionFill | None:
    return cast(ExecutionFill | None, next(iter(db0.find(ExecutionFill, db0.as_tag(intent))), None))


def initialize_accounting(state: TraderState) -> None:
    """Migrate only confirmed local accounting checkpoints, never legacy account valuations."""
    version = LocalAccountingVersion()
    if version.initialized:
        return
    net = Decimal(0)
    for intent in db0.find(Intent, ExecutionState.FILLED):
        if intent.operation not in {Operation.open, Operation.close}:
            continue
        fill = fill_for(intent)
        if fill is None:
            fill = ExecutionFill(intent)
            db0.tags(fill).add("EXECUTION_FILL")
            fill.cost_reservation_released = True  # Legacy projection already released its estimate.
            checkpoint = next(iter(db0.find(OperationContribution, db0.as_tag(intent))), None)
            if checkpoint is not None:
                fill.costs = checkpoint.costs
                if intent.operation == Operation.close:
                    fill.gross_pnl = checkpoint.realized_pnl
        fill.budget_net = (fill.gross_pnl or Decimal(0)) if intent.operation == Operation.close else Decimal(0)
        fill.budget_net -= fill.costs or Decimal(0)
        net += fill.budget_net
    state.strategy_realized = money(net, allow_negative=True)
    version.initialized = True


def record_fill(intent: Intent, outcome: BrokerOutcome, state: TraderState) -> None:
    if intent.state != ExecutionState.FILLED or intent.operation not in {Operation.open, Operation.close}:
        return
    fill = fill_for(intent)
    if fill is None:
        fill = ExecutionFill(intent)
        db0.tags(fill).add("EXECUTION_FILL")
    for name, value in (
        ("units", outcome.filled_units),
        ("price", outcome.execution_price),
        ("costs", outcome.actual_cost_usd),
        ("gross_pnl", outcome.realized_pnl_usd),
    ):
        if value is not None and value.is_finite() and (name == "gross_pnl" or value >= 0):
            setattr(fill, name, value)
    if (
        intent.operation == Operation.close
        and fill.gross_pnl is None
        and fill.price is not None
        and fill.units is not None
    ):
        position = next(iter(db0.find(Position, str(intent.params["position_id"]))), None)
        entry = None if position is None else fill_for(position.intent)
        if position is not None and entry is not None and entry.price is not None:
            fill.gross_pnl = money(
                fill.units * (fill.price - entry.price) * (1 if str(position.side) == "long" else -1),
                allow_negative=True,
            )
    if intent.operation == Operation.open and fill.costs is not None and not fill.cost_reservation_released:
        state.strategy_committed = max(
            Decimal(0), state.strategy_committed - Decimal(intent.params.get("estimated_strategy_cost_usd", 0))
        )
        fill.cost_reservation_released = True
    # Costs and gross close P/L are applied once, including late confirmations.
    net = (fill.gross_pnl or Decimal(0)) if intent.operation == Operation.close else Decimal(0)
    net -= fill.costs or Decimal(0)
    state.strategy_realized = money(state.strategy_realized + net - fill.budget_net, allow_negative=True)
    fill.budget_net = net


def position_value(position: Position) -> PositionValue:
    entry = fill_for(position.intent)
    price = next(iter(db0.find(PositionPrice, db0.as_tag(position))), None)
    units = None if entry is None else entry.units
    if units is not None:
        for close in db0.find(Intent, Operation.close, ExecutionState.FILLED):
            if close.params.get("position_id") != position.position_id:
                continue
            fill = fill_for(close)
            if fill is None or fill.units is None:
                units = None
                break
            units -= fill.units
        if units is not None and units < 0:
            units = None
    entry_price = None if entry is None else entry.price
    liquidation = None if price is None else (price.bid if str(position.side) == "long" else price.ask)
    pnl = None
    if units is not None and entry_price is not None and liquidation is not None:
        pnl = money(
            units * (liquidation - entry_price) * (1 if str(position.side) == "long" else -1), allow_negative=True
        )
    return PositionValue(entry_price, liquidation, units, pnl, None if price is None else price.refreshed_at)


def totals(initialized: bool) -> tuple[Decimal | None, Decimal | None, datetime | None]:
    realized_value = Decimal(0)
    accounting_missing = not initialized
    exposure_missing = False
    for intent in db0.find(Intent, ExecutionState.FILLED):
        if intent.operation not in {Operation.open, Operation.close}:
            continue
        if intent.operation == Operation.open and not next(iter(db0.find(Position, db0.as_tag(intent))), None):
            exposure_missing = True
        fill = fill_for(intent)
        if fill is None or fill.costs is None or (intent.operation == Operation.close and fill.gross_pnl is None):
            accounting_missing = True
            continue
        realized_value += (fill.gross_pnl or Decimal(0)) if intent.operation == Operation.close else Decimal(0)
        realized_value -= fill.costs
    if next(iter(db0.find(Intent, ExecutionState.UNKNOWN)), None) is not None:
        accounting_missing = True
    realized = None if accounting_missing else realized_value
    values = [position_value(p) for p in db0.find(Position, PositionState.OPEN)]
    unrealized = (
        sum((v.unrealized_pnl for v in values if v.unrealized_pnl is not None), Decimal(0))
        if initialized and not exposure_missing and all(v.unrealized_pnl is not None for v in values)
        else None
    )
    timestamps = [v.price_refreshed_at for v in values if v.price_refreshed_at is not None]
    return realized, unrealized, min(timestamps) if timestamps else None
