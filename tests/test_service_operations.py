from __future__ import annotations

import json

import pytest

from trader_api.domain import IntentState
from trader_api.errors import TraderError
from trader_api.service import TraderService
from trader_api.storage import Preview


def _service(runtime: dict[str, object]) -> TraderService:
    return TraderService(
        "alpha",
        str(runtime["profile"]),
        scope_verifier=runtime["verifier"],  # type: ignore[arg-type]
        broker=runtime["broker"],  # type: ignore[arg-type]
        storage_root=runtime["root"],  # type: ignore[arg-type]
    )


def _open(service: TraderService) -> dict[str, object]:
    preview = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    return service.submit(preview["preview_id"], "open")


def test_modify_partial_close_and_owner_accounting_remain_separate(
    runtime: dict[str, object],
) -> None:
    service = _service(runtime)
    service.initialize("2000")
    _open(service)
    modify = service.preview_modify(position_id="601", stop_loss_rate="90")
    modified = service.submit(modify["preview_id"], "modify")
    assert modified["state"] == "ACKNOWLEDGED"
    assert service.positions()[0]["stop_loss_rate"] == "90.00"
    closing = service.preview_close(position_id="601", fraction="0.5")
    closed = service.submit(closing["preview_id"], "close")
    assert closed["state"] == "FILLED"
    assert service.positions()[0]["strategy_notional_usd"] == "50.00"
    history = service.portfolio_history()
    assert len(history["series"]["strategy"]) == 1
    assert history["series"]["owner_mirror"] == []


def test_pending_order_can_be_canceled_and_releases_reservation(runtime: dict[str, object]) -> None:
    broker = runtime["broker"]
    broker.outcome_state = IntentState.ACKNOWLEDGED  # type: ignore[attr-defined]
    service = _service(runtime)
    service.initialize("2000")
    _open(service)
    assert service.orders()[0]["order_id"] == "501"
    cancel = service.preview_cancel(order_id="501")
    canceled = service.submit(cancel["preview_id"], "cancel")
    assert canceled["state"] == "CANCELED"
    assert service.orders() == []
    assert service.trader_status()["strategy_budget"]["committed_usd"] == "0.00"


def test_foreign_entities_have_indistinguishable_not_found(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    service.initialize("2000")
    for operation in (
        lambda: service.preview_close(position_id="foreign"),
        lambda: service.preview_cancel(order_id="foreign"),
    ):
        try:
            operation()
        except TraderError as exc:
            assert exc.code == "NOT_FOUND"
            assert exc.message.endswith("not found")
        else:
            raise AssertionError("foreign entity unexpectedly visible")


def test_owner_reconciliation_posts_only_actual_mirror_values(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    service.initialize("2000")
    _open(service)
    result = runtime["admin"].record_mirror_reconciliation(  # type: ignore[union-attr]
        trader_id="alpha",
        actual_realized_pnl_usd="3.25",
        actual_committed_usd="19.75",
        copy_healthy=True,
    )
    assert result["actual_committed_usd"] == "19.75"
    status = service.trader_status()
    assert status["owner_mirror_budget"]["committed_usd"] == "19.75"
    owner_entries = service.portfolio_history()["series"]["owner_mirror"]
    assert owner_entries[0]["amount_usd"] == "3.25"


def test_full_close_omits_units_but_partial_close_sends_them(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    service.initialize("2000")
    _open(service)
    full = service.preview_close(position_id="601", fraction="1")
    full_params = json.loads(service._api_reference(full["preview_id"], Preview).params_json)
    assert "UnitsToDeduct" not in service._mutation("close", full_params, "request").payload
    partial = service.preview_close(position_id="601", fraction="0.5")
    partial_params = json.loads(service._api_reference(partial["preview_id"], Preview).params_json)
    assert service._mutation("close", partial_params, "request").payload["UnitsToDeduct"]


def test_local_suspension_blocks_exposure_but_preserves_risk_reduction(
    runtime: dict[str, object],
) -> None:
    service = _service(runtime)
    service.initialize("2000")
    _open(service)
    runtime["admin"].set_suspended(trader_id="alpha", suspended=True)  # type: ignore[union-attr]
    with pytest.raises(TraderError) as opening:
        service.preview_open(symbol="MSFT", side="long", strategy_notional_usd="100")
    assert opening.value.code == "PORTFOLIO_SUSPENDED"
    close = service.preview_close(position_id="601")
    assert service.submit(close["preview_id"], "suspended-close")["state"] == "FILLED"
