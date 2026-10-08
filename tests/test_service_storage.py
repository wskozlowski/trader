from __future__ import annotations

from decimal import Decimal

import dbzero as db0
import pytest

from trader_api.errors import TraderError
from trader_api.service import TraderService
from trader_api.storage import (
    AuditEvent,
    Currency,
    Environment,
    Intent,
    LedgerEntry,
    Order,
    PortfolioBinding,
    Position,
    Reservation,
    Trader,
    TraderRegistration,
    TraderState,
    close_dbzero,
)


def _service(runtime: dict[str, object]) -> TraderService:
    return TraderService(
        "alpha",
        str(runtime["profile"]),
        scope_verifier=runtime["verifier"],  # type: ignore[arg-type]
        broker=runtime["broker"],  # type: ignore[arg-type]
        storage_root=runtime["root"],  # type: ignore[arg-type]
    )


def test_lifecycle_persists_preview_intent_and_audit(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    activated = service.initialize("2000")
    assert activated["owner_copy_investment_usd"] == "2000.00"
    assert activated["strategy_virtual_balance_usd"] == "10000.00"
    preview = service.preview_open(
        symbol="AAPL",
        side="long",
        order_type="market",
        strategy_notional_usd="100",
        leverage=1,
    )
    result = service.submit(preview["preview_id"], "open-one")
    assert result["strategy_execution_state"] == "FILLED"
    assert result["mirror_reconciliation_state"] == "PENDING"
    assert service.positions()[0]["position_id"] == "601"
    assert service.verify_audit()["valid"] is True

    close_dbzero()
    restarted = _service(runtime)
    assert restarted.intent_status(result["intent_id"])["state"] == "FILLED"
    assert restarted.positions()[0]["symbol"] == "AAPL"
    assert restarted.verify_audit()["valid"] is True
    assert restarted.portfolio()["environment"] == "demo"

    store, prefix = restarted.store, restarted.prefix
    assert store is not None and prefix is not None
    trader = restarted.trader
    assert trader is not None and trader.name == "alpha"
    assert store.all(Trader, "alpha", prefix=prefix) == [trader]
    registration = store.one(TraderRegistration, db0.as_tag(trader), prefix=store.control_prefix)
    assert registration is not None and registration.trader == trader
    state = store.state(prefix, trader)
    assert state.trader == trader
    for field_name in (
        "strategy_initial_cap",
        "owner_initial_cap",
        "strategy_realized",
        "owner_realized",
        "strategy_committed",
        "owner_committed",
    ):
        assert isinstance(getattr(state, field_name), Decimal)
    assert state.strategy_realized == Decimal("-1.50")
    binding = store.binding(prefix)
    assert isinstance(binding.investment_usd, Decimal)
    assert isinstance(binding.virtual_balance_usd, Decimal)
    assert binding.investment_usd == Decimal("2000.00")
    assert state.environment == Environment.DEMO
    assert state.currency == Currency.USD
    assert state.currency != "USD"
    assert restarted.initialize("2000")["currency"] == "USD"
    assert state.environment != "demo"
    assert store.binding(prefix).environment == Environment.DEMO
    assert store.binding(prefix).trader == trader
    assert store.one(TraderState, db0.as_tag(trader), prefix=prefix) is not None
    assert store.one(PortfolioBinding, db0.as_tag(trader), prefix=prefix) is not None
    assert store.register("alpha", "service-secret") == registration
    assert store.all(Trader, prefix=prefix) == [trader]
    intent = store.one(Intent, prefix=prefix)
    assert intent is not None
    assert str(db0.uuid(intent)) == result["intent_id"]
    assert str(db0.uuid(intent.preview)) == preview["preview_id"]
    assert store.one(Intent, db0.as_tag(intent.preview), prefix=prefix) == intent
    assert store.one(Intent, intent.request_id, prefix=prefix) == intent
    reservation = store.one(Reservation, db0.as_tag(intent), prefix=prefix)
    assert reservation is not None
    assert isinstance(reservation.strategy_amount_usd, Decimal)
    assert isinstance(reservation.owner_amount_usd, Decimal)
    position = store.one(Position, db0.as_tag(intent), prefix=prefix)
    assert position is not None
    assert isinstance(position.strategy_notional_usd, Decimal)
    assert isinstance(position.units, Decimal)
    assert position.stop_loss_rate is None and position.take_profit_rate is None
    entry = store.one(LedgerEntry, db0.as_tag(intent), prefix=prefix)
    assert entry is not None and isinstance(entry.amount_usd, Decimal)
    assert entry.amount_usd == Decimal("100.00")
    for model in (Reservation, Order, Position, LedgerEntry, AuditEvent):
        linked = store.all(model, db0.as_tag(intent), prefix=prefix)
        assert linked, model
        assert all(item.intent == intent for item in linked)
        assert all(db0.get_prefix_of(item).name == prefix.lstrip("/") for item in linked)
        assert store.all(model, prefix=store.control_prefix) == []
    assert store.verify_control_audit()["valid"] is True
    assert any(event["intent_id"] == result["intent_id"] for event in restarted.audit_events())
    assert restarted.portfolio_history()["series"]["strategy"][0]["intent_id"] == result["intent_id"]


def test_idempotency_conflict_and_copy_gate(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    service.initialize("2000")
    preview = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    first = service.submit(preview["preview_id"], "same-key")
    assert service.submit(preview["preview_id"], "same-key") == first
    assert service.trader_status()["copy_healthy"] is False


def test_unverified_context_has_explicit_readiness(runtime: dict[str, object]) -> None:
    service = TraderService(
        "alpha",
        str(runtime["profile"]),
        broker=runtime["broker"],  # type: ignore[arg-type]
    )
    capabilities = service.capabilities()
    assert capabilities["environment"] is None
    assert capabilities["verification_error"] == "ENVIRONMENT_UNVERIFIED"
    assert all(value is False for value in capabilities["effective"].values())
    assert service.trader_status()["readiness_error"] == "ENVIRONMENT_UNVERIFIED"


def test_api_reference_rejects_invalid_and_wrong_type_ids(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    service.initialize("2000")
    preview = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    for identifier in ("invalid", "", preview["preview_id"]):
        with pytest.raises(TraderError) as error:
            service.intent_status(identifier)
        assert error.value.code == "NOT_FOUND"


def test_currency_enum_includes_pln_and_persists(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    assert {str(value) for value in Currency.values()} == {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "PLN"}
    assert service.store is not None and service.prefix is not None and service.trader is not None
    state = service.store.state(service.prefix, service.trader)
    state.currency = Currency.PLN
    service.store.commit(service.prefix)

    close_dbzero()
    restarted = _service(runtime)
    assert restarted.store is not None and restarted.prefix is not None and restarted.trader is not None
    assert restarted.store.state(restarted.prefix, restarted.trader).currency == Currency.PLN
    assert restarted.trader_status()["currency"] == "PLN"
    with pytest.raises(TraderError) as error:
        restarted.initialize("2000", "PLN")
    assert error.value.code == "UNSUPPORTED_CURRENCY"
