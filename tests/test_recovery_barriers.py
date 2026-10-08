from __future__ import annotations

from decimal import Decimal

import pytest

from trader_api.domain import BrokerOutcome, IntentState
from trader_api.service import TraderService
from trader_api.storage import close_dbzero


def _service(runtime: dict[str, object], hook: object | None = None) -> TraderService:
    return TraderService(
        "alpha",
        str(runtime["profile"]),
        scope_verifier=runtime["verifier"],  # type: ignore[arg-type]
        broker=runtime["broker"],  # type: ignore[arg-type]
        storage_root=runtime["root"],  # type: ignore[arg-type]
        barrier_hook=hook,  # type: ignore[arg-type]
    )


def _crashing_hook(target: str):
    def hook(name: str) -> None:
        if name == target:
            raise RuntimeError("injected crash")

    return hook


def _preview(service: TraderService) -> str:
    service.initialize("2000")
    return service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")["preview_id"]


def test_recovery_cancels_intent_without_control_reservation(runtime: dict[str, object]) -> None:
    service = _service(runtime, _crashing_hook("trader_intent_committed"))
    with pytest.raises(RuntimeError, match="injected"):
        service.submit(_preview(service), "crash-one")
    close_dbzero()
    restarted = _service(runtime)
    result = restarted.reconcile()
    assert result["recovered_intents"] == 1
    assert restarted.trader_status()["strategy_budget"]["committed_usd"] == "0.00"
    assert any(event["kind"] == "ORPHAN_INTENT_CANCELED" for event in restarted.audit_events())


def test_recovery_keeps_control_reservation_when_lookup_is_ambiguous(
    runtime: dict[str, object],
) -> None:
    service = _service(runtime, _crashing_hook("control_reservation_committed"))
    with pytest.raises(RuntimeError, match="injected"):
        service.submit(_preview(service), "crash-two")
    close_dbzero()
    restarted = _service(runtime)
    result = restarted.reconcile()
    assert result["unresolved_intents"] == 1
    assert restarted.trader_status()["strategy_budget"]["committed_usd"] == "102.00"


def test_recovery_replays_control_outcome_into_trader_prefix(runtime: dict[str, object]) -> None:
    service = _service(runtime, _crashing_hook("control_outcome_committed"))
    with pytest.raises(RuntimeError, match="injected"):
        service.submit(_preview(service), "crash-three")
    close_dbzero()
    restarted = _service(runtime)
    result = restarted.reconcile()
    assert result["recovered_intents"] == 1
    assert restarted.positions()[0]["position_id"] == "601"
    assert restarted.verify_audit()["valid"] is True


def test_reconcile_polls_acknowledged_order_to_fill(runtime: dict[str, object]) -> None:
    broker = runtime["broker"]
    broker.outcome_state = IntentState.ACKNOWLEDGED  # type: ignore[attr-defined]
    service = _service(runtime)
    submitted = service.submit(_preview(service), "ack-then-fill")
    assert submitted["state"] == "ACKNOWLEDGED"

    def filled(context: object, order_id: str) -> BrokerOutcome:
        return BrokerOutcome(
            IntentState.FILLED,
            "lookup-reference",
            broker_order_id=order_id,
            broker_position_id="777",
            filled_units=Decimal("0.5"),
            actual_cost_usd=Decimal("1.50"),
        )

    broker.lookup_order = filled  # type: ignore[attr-defined,method-assign]
    result = service.reconcile()
    assert result["recovered_intents"] == 1
    assert service.positions()[0]["position_id"] == "777"
    assert service.orders() == []
