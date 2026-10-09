"""Small immutable presentation boundary types."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from ..ui_api.types import OrderView, PositionView


@dataclass(frozen=True, slots=True)
class PositionRow:
    row_key: str
    symbol: str
    side: str
    state: str
    notional: Decimal
    units: Decimal
    leverage: int
    stop_loss: Decimal | None
    take_profit: Decimal | None


@dataclass(frozen=True, slots=True)
class OrderRow:
    row_key: str
    order_id: str
    symbol: str
    state: str


def position_row(value: PositionView) -> PositionRow:
    return PositionRow(
        uuid4().hex,
        value.symbol,
        str(value.side),
        str(value.state),
        value.strategy_notional_usd,
        value.units,
        value.leverage,
        value.stop_loss_rate,
        value.take_profit_rate,
    )


def order_row(value: OrderView) -> OrderRow:
    return OrderRow(uuid4().hex, value.order_id, value.symbol, str(value.state))
