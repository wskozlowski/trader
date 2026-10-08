from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import Budget, money, reserve_money
from .errors import TraderError


@dataclass(frozen=True, slots=True)
class Admission:
    strategy_reservation: Decimal
    owner_reservation: Decimal
    strategy_remaining: Decimal
    owner_remaining: Decimal
    estimated_owner_notional: Decimal


def copied_notional(strategy_notional: Decimal, investment: Decimal, virtual_balance: Decimal) -> Decimal:
    if virtual_balance <= 0 or investment <= 0:
        raise TraderError("RECONCILIATION_REQUIRED", "copy exposure cannot be conservatively bounded")
    return reserve_money(strategy_notional * investment / virtual_balance)


def admit_open(
    *,
    strategy: Budget,
    owner: Budget,
    strategy_notional: Decimal,
    strategy_cost_buffer: Decimal,
    owner_cost_buffer: Decimal,
    investment: Decimal,
    virtual_balance: Decimal,
) -> Admission:
    strategy_reservation = reserve_money(strategy_notional + strategy_cost_buffer)
    owner_notional = copied_notional(strategy_notional, investment, virtual_balance)
    owner_reservation = reserve_money(owner_notional + owner_cost_buffer)
    if strategy_reservation > strategy.available_to_open or owner_reservation > owner.available_to_open:
        raise TraderError("INSUFFICIENT_BUDGET", "the operation exceeds an isolated portfolio budget")
    return Admission(
        strategy_reservation,
        owner_reservation,
        money(strategy.available_to_open - strategy_reservation),
        money(owner.available_to_open - owner_reservation),
        owner_notional,
    )
