from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trader_api.errors import TraderError
from trader_api.storage import (
    AuditEvent,
    BaseEvent,
    ControlEvent,
    ControlReservation,
    ExecutionState,
    Intent,
    Lifecycle,
    Operation,
    Preview,
    close_dbzero,
)

from .test_service_storage import _service


def test_native_preview_and_outcome_survive_restart(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    service.initialize("2000")
    response = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    close_dbzero()
    service = _service(runtime)
    preview = service._api_reference(response["preview_id"], Preview)
    assert preview.operation == Operation.open
    assert service.store is not None and service.prefix is not None
    assert service.store.one(Preview, Operation.open, prefix=service.prefix) == preview
    assert isinstance(preview.created_at, datetime)
    assert preview.created_at.tzinfo == UTC
    assert preview.expires_at - preview.created_at == timedelta(minutes=5)
    assert preview.created_at.isoformat() == response["created_at"]
    assert preview.params["strategy_notional_usd"] == Decimal("100.00")
    assert isinstance(preview.params["strategy_notional_usd"], Decimal)
    assert preview.params["broker_payload"]["instrumentId"] == 1001

    result = service.submit(response["preview_id"], "native-models")
    intent = service._api_reference(result["intent_id"], Intent)
    assert intent.state == ExecutionState.FILLED
    assert isinstance(intent.created_at, datetime)
    preview.params["strategy_notional_usd"] = Decimal("999.00")
    preview.params["broker_payload"]["instrumentId"] = 9999
    assert intent.params["strategy_notional_usd"] == Decimal("100.00")
    assert intent.params["broker_payload"]["instrumentId"] == 1001
    assert service.store is not None and service.prefix is not None
    service.store.commit(service.prefix)
    close_dbzero()
    service = _service(runtime)
    assert service.store is not None
    control = service.store.one(ControlReservation, prefix=service.store.control_prefix)
    assert control is not None and control.outcome is not None
    assert control.state == ExecutionState.FILLED
    assert control.outcome["actual_cost_usd"] == Decimal("1.50")
    assert isinstance(control.outcome["actual_cost_usd"], Decimal)
    assert service._objects()[1].lifecycle == Lifecycle.ACTIVE
    # The direct Python API must also expose only JSON-compatible values.
    json.dumps(
        [
            result,
            response,
            service.trader_status(),
            service.portfolio(),
            service.audit_events(),
            service.positions(),
            service.portfolio_history(),
            service.capabilities(),
        ]
    )


def test_native_preview_expiration(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    service.initialize("2000")
    response = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    expiration = service._api_reference(response["preview_id"], Preview).expires_at
    service._clock = lambda: expiration
    with pytest.raises(TraderError) as error:
        service.submit(response["preview_id"], "expired")
    assert error.value.code == "STALE_PREVIEW"


def test_native_audit_facts_hash_after_restart_and_reject_tampering(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    assert service.store is not None and service.prefix is not None and service.trader is not None
    store = service.store
    state = store.state(service.prefix, service.trader)
    facts = {
        "nested": {"amount": Decimal("1.23"), "at": datetime(2026, 1, 2, microsecond=123456, tzinfo=UTC)},
        "flags": [True, None, 3],
    }
    store.append_audit(service.prefix, state, kind="NATIVE_FACTS", actor="test", facts=facts)
    store.commit(service.prefix)
    store.append_control_event("NATIVE_FACTS", facts)
    store.commit(store.control_prefix)
    facts["nested"]["amount"] = Decimal("9.99")
    close_dbzero()
    service = _service(runtime)
    assert service.store is not None and service.prefix is not None
    store = service.store
    assert service.verify_audit()["valid"] is True
    assert store.verify_control_audit()["valid"] is True
    event = next(item for item in store.all(AuditEvent, prefix=service.prefix) if item.kind == "NATIVE_FACTS")
    assert isinstance(event, BaseEvent)
    assert event.actor == "test"
    assert event.intent is None
    assert event.source == "trader"
    with pytest.raises(AttributeError, match="immutable memo"):
        event.kind = "TAMPERED"
    with pytest.raises(AttributeError, match="immutable memo"):
        event.actor = "TAMPERED"
    assert isinstance(event.occurred_at, datetime)
    assert event.facts["nested"]["amount"] == Decimal("1.23")
    assert event.facts["nested"]["at"].microsecond == 123000
    with pytest.raises(TypeError):
        event.facts["nested"]["amount"] = Decimal("8.00")
    assert service.verify_audit()["valid"] is True
    control = next(item for item in store.all(ControlEvent, prefix=store.control_prefix) if item.kind == "NATIVE_FACTS")
    assert isinstance(control, BaseEvent)
    assert isinstance(control.occurred_at, datetime)
    assert control.facts["nested"]["amount"] == Decimal("1.23")
    with pytest.raises(AttributeError, match="immutable memo"):
        control.kind = "TAMPERED"
    with pytest.raises(TypeError):
        control.facts["flags"] = [False, None, 3]
    assert store.verify_control_audit()["valid"] is True


def test_token_dates_are_native_and_expiry_can_be_absent(runtime: dict[str, object]) -> None:
    service = _service(runtime)
    assert service.store is not None and service._context is not None
    fingerprint = service._context.credential_fingerprint
    evidence = replace(runtime["evidence"], expires_at=None)
    service.store.record_scope_evidence(fingerprint, evidence)
    close_dbzero()
    service = _service(runtime)
    assert service.store is not None
    record = service.store.scope_evidence(fingerprint)
    assert record is not None
    assert isinstance(record.issued_at, datetime)
    assert record.issued_at.tzinfo == UTC
    assert record.expires_at is None
