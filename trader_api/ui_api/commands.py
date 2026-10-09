"""Synchronous, typed command facade for the native UI.

This module deliberately keeps memo handles and preview identifiers server-side.
It is framework independent; web controllers can safely offload these calls.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ..domain import Side
from ..errors import TraderError
from ..storage import Currency, Order, Position
from .session import Session, check_handle, selected


class OpeningOrderType(StrEnum):
    MARKET = "market"
    MARKET_IF_TOUCHED = "market_if_touched"
    LIMIT_IOC = "limit_ioc"


@dataclass(frozen=True, slots=True)
class OpenOrderRequest:
    symbol: str | None
    instrument_id: int | None
    side: Side
    order_type: OpeningOrderType
    strategy_notional_usd: Decimal
    leverage: int
    trigger_rate: Decimal | None = None
    limit_rate: Decimal | None = None
    stop_loss_rate: Decimal | None = None
    take_profit_rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    order: Order


@dataclass(frozen=True, slots=True)
class ModifyPositionRequest:
    position: Position
    stop_loss_rate: Decimal | None
    take_profit_rate: Decimal | None
    stop_loss_type: str | None = None


@dataclass(frozen=True, slots=True)
class ClosePositionRequest:
    position: Position
    fraction: Decimal


@dataclass(frozen=True, slots=True)
class ActivationRequest:
    expected_investment_usd: Decimal
    currency: Currency = Currency.USD


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    operation: str
    created_at: datetime
    expires_at: datetime
    review: dict[str, Any]
    _session: Session = field(repr=False, compare=False)
    _preview_id: str = field(repr=False, compare=False)
    _idempotency_key: str = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CommandResult:
    operation: str
    state: str
    error_code: str | None
    broker_order_id: str | None
    broker_position_id: str | None
    request_id: str | None
    _intent_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ActivationResult:
    value: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    recovered_intents: int
    unresolved_intents: int
    requires_owner_reconciliation: bool
    raw: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, slots=True)
class CommandAvailability:
    activation: bool
    opening: bool
    close: bool
    cancel: bool
    protection: bool
    execution_reconciliation: bool
    reasons: dict[str, str]


def _position_id(value: Position) -> str:
    return str(getattr(value, "position_id", ""))


def _order_id(value: Order) -> str:
    return str(getattr(value, "order_id", ""))


def _prepared(session: Session, raw: dict[str, Any]) -> PreparedCommand:
    key = uuid.uuid4().hex
    safe = {k: v for k, v in raw.items() if k not in {"preview_id", "broker_payload", "broker_state_fingerprint"}}
    return PreparedCommand(
        str(raw["operation"]),
        datetime.fromisoformat(str(raw["created_at"])),
        datetime.fromisoformat(str(raw["expires_at"])),
        safe,
        session,
        str(raw["preview_id"]),
        key,
    )


def _preview(session: Session, operation: str, **kwargs: Any) -> PreparedCommand:
    with selected(session):
        pass  # Validate the captured database lifetime before network work.
    service = session._service
    preview_methods: dict[str, Callable[..., dict[str, Any]]] = {
        "open": service.preview_open,
        "close": service.preview_close,
        "modify": service.preview_modify,
        "cancel": service.preview_cancel,
    }
    raw = preview_methods[operation](**kwargs)
    return _prepared(session, raw)


def prepare_open(session: Session, request: OpenOrderRequest) -> PreparedCommand:
    if (request.symbol is None) == (request.instrument_id is None):
        raise TraderError("INVALID_ORDER", "provide exactly one symbol or instrument id")
    if (
        not isinstance(request.strategy_notional_usd, Decimal)
        or not request.strategy_notional_usd.is_finite()
        or request.strategy_notional_usd <= 0
    ):
        raise TraderError("INVALID_AMOUNT", "notional must be a finite positive decimal")
    if request.leverage < 1:
        raise TraderError("INVALID_ORDER", "leverage must be positive")
    return _preview(
        session,
        "open",
        symbol=request.symbol,
        instrument_id=request.instrument_id,
        side=str(request.side),
        order_type=str(request.order_type),
        strategy_notional_usd=request.strategy_notional_usd,
        leverage=request.leverage,
        trigger_rate=request.trigger_rate,
        limit_rate=request.limit_rate,
        stop_loss_rate=request.stop_loss_rate,
        take_profit_rate=request.take_profit_rate,
    )


def prepare_cancel(session: Session, request: CancelOrderRequest) -> PreparedCommand:
    with selected(session):
        check_handle(session, request.order, Order)
        order_id = _order_id(request.order)
    return _preview(session, "cancel", order_id=order_id)


def prepare_modify(session: Session, request: ModifyPositionRequest) -> PreparedCommand:
    with selected(session):
        check_handle(session, request.position, Position)
        position_id = _position_id(request.position)
    return _preview(
        session,
        "modify",
        position_id=position_id,
        stop_loss_rate=request.stop_loss_rate,
        take_profit_rate=request.take_profit_rate,
        stop_loss_type=request.stop_loss_type,
    )


def prepare_close(session: Session, request: ClosePositionRequest) -> PreparedCommand:
    with selected(session):
        check_handle(session, request.position, Position)
        position_id = _position_id(request.position)
    if not isinstance(request.fraction, Decimal) or not request.fraction.is_finite() or not 0 < request.fraction <= 1:
        raise TraderError("INVALID_AMOUNT", "fraction must be in (0, 1]")
    return _preview(session, "close", position_id=position_id, fraction=request.fraction)


def submit_prepared(session: Session, prepared: PreparedCommand) -> CommandResult:
    if prepared._session is not session:
        raise TraderError("INVALID_HANDLE", "prepared command belongs to another session")
    with selected(session):
        pass
    raw = session._service.submit(prepared._preview_id, prepared._idempotency_key)
    if session._worker is not None:
        session._worker.request(execution_changed=True)
    return CommandResult(
        str(raw.get("operation", prepared.operation)),
        str(raw.get("state", "UNKNOWN")),
        raw.get("error_code"),
        raw.get("broker_order_id"),
        raw.get("broker_position_id"),
        raw.get("request_id"),
        str(raw.get("intent_id", "")),
    )


def activate(session: Session, request: ActivationRequest) -> ActivationResult:
    if (
        not isinstance(request.expected_investment_usd, Decimal)
        or not request.expected_investment_usd.is_finite()
        or request.expected_investment_usd < 0
    ):
        raise TraderError("INVALID_AMOUNT", "investment must be a finite decimal")
    with selected(session):
        pass
    value = session._service.initialize(request.expected_investment_usd, str(request.currency))
    return ActivationResult(value)


def check_executions(session: Session) -> ReconciliationResult:
    with selected(session):
        pass
    raw = session._service.reconcile()
    if session._worker is not None:
        session._worker.request(execution_changed=True)
    return ReconciliationResult(
        int(raw.get("recovered", raw.get("recovered_intents", 0))),
        int(raw.get("unresolved", raw.get("unresolved_intents", 0))),
        bool(raw.get("requires_owner_reconciliation", raw.get("mirror") == "requires_owner_reconciliation")),
        raw,
    )


def get_command_availability(session: Session) -> CommandAvailability:
    with selected(session):
        caps = session._service.capabilities()
    effective = caps.get("effective", {})
    initialized = bool(caps.get("initialized"))
    verified = bool(caps.get("environment_verified")) and (
        caps.get("trading_mode") != "standalone" or bool(caps.get("direct_account_verified"))
    )
    writable = bool(caps.get("scope_write"))
    lifecycle = str(caps.get("lifecycle", ""))
    reasons: dict[str, str] = {}
    if not verified:
        reasons["all"] = str(caps.get("verification_error") or caps.get("readiness_error") or "FAILED_STARTUP")
    if not writable:
        reasons["write"] = "PERMISSION_REQUIRED"
    return CommandAvailability(
        activation=verified and lifecycle == "READY" and not initialized and writable,
        opening=verified and initialized and writable and bool(effective.get("market")),
        close=verified
        and initialized
        and writable
        and lifecycle in {"ACTIVE", "SUSPENDED"}
        and bool(effective.get("partial_close")),
        cancel=verified
        and initialized
        and writable
        and lifecycle in {"ACTIVE", "SUSPENDED"}
        and bool(effective.get("cancel") or effective.get("cancel_close")),
        protection=verified
        and initialized
        and writable
        and lifecycle in {"ACTIVE", "SUSPENDED"}
        and bool(effective.get("modify_protection")),
        execution_reconciliation=verified and initialized and writable,
        reasons=reasons,
    )
