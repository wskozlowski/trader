from __future__ import annotations

from decimal import Decimal

import pytest

from trader_api.accounting import admit_open
from trader_api.broker.base import InstrumentSizing
from trader_api.broker.etoro import open_payload
from trader_api.domain import Budget
from trader_api.errors import TraderError


def test_dual_budget_admission_uses_conservative_copy_ratio() -> None:
    result = admit_open(
        strategy=Budget(Decimal("2000"), Decimal("0"), Decimal("0")),
        owner=Budget(Decimal("2000"), Decimal("0"), Decimal("0")),
        strategy_notional=Decimal("1000"),
        strategy_cost_buffer=Decimal("2.001"),
        owner_cost_buffer=Decimal("0.401"),
        investment=Decimal("2000"),
        virtual_balance=Decimal("10000"),
    )
    assert result.strategy_reservation == Decimal("1002.01")
    assert result.estimated_owner_notional == Decimal("200.00")
    assert result.owner_reservation == Decimal("200.41")


def test_short_mapping_and_amount_are_not_full_notional() -> None:
    sizing = InstrumentSizing(1, Decimal("250"), Decimal("10"), Decimal("1000"), Decimal("10"), Decimal("100"), "CFD")
    payload = open_payload(
        sizing=sizing,
        side="short",
        order_type="market",
        leverage=4,
        trigger_rate=None,
        limit_rate=None,
        stop_loss_rate=Decimal("110"),
        take_profit_rate=None,
    )
    assert payload["transaction"] == "sellShort"
    assert payload["amount"] == "250"


def test_leveraged_open_requires_stop_loss() -> None:
    sizing = InstrumentSizing(1, Decimal("500"), Decimal("10"), Decimal("1000"), Decimal("10"), Decimal("100"), "CFD")
    with pytest.raises(TraderError, match="stop-loss"):
        open_payload(
            sizing=sizing,
            side="long",
            order_type="market",
            leverage=2,
            trigger_rate=None,
            limit_rate=None,
            stop_loss_rate=None,
            take_profit_rate=None,
        )
