from __future__ import annotations

from trader_api.service import TraderService
from trader_api.storage import close_dbzero


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
