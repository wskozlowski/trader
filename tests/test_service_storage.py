from __future__ import annotations

import dbzero as db0
import pytest

from trader_api.errors import TraderError
from trader_api.service import TraderService
from trader_api.storage import AuditEvent, Intent, LedgerEntry, Order, Position, Reservation, close_dbzero


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

    store, prefix = restarted.store, restarted.prefix
    assert store is not None and prefix is not None
    intent = store.one(Intent, prefix=prefix)
    assert intent is not None
    assert str(db0.uuid(intent)) == result["intent_id"]
    assert str(db0.uuid(intent.preview)) == preview["preview_id"]
    assert store.one(Intent, db0.as_tag(intent.preview), prefix=prefix) == intent
    assert store.one(Intent, intent.request_id, prefix=prefix) == intent
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
