from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, cast

import dbzero as db0  # type: ignore[import-untyped]

from .accounting import admit_open
from .auth import ControlScopeVerifier, ScopeVerifier, fingerprint, resolve_context
from .broker import BrokerAdapter, EtoroBrokerAdapter
from .broker.etoro import open_payload
from .config import Profile, fixed_storage_root, load_profile, route_environment
from .domain import (
    BrokerMutation,
    BrokerOutcome,
    Budget,
    Environment,
    IntentState,
    VerifiedContext,
    money,
    reserve_money,
    utc_now,
)
from .errors import TraderError
from .portfolio import fill_for, initialize_accounting, position_value, record_fill
from .serialization import api_value, canonical_json, native_copy, storage_datetime
from .storage import (
    AuditEvent,
    Currency,
    DbzeroStore,
    DirectAccount,
    ExecutionState,
    Intent,
    LedgerDomain,
    LedgerEntry,
    LedgerKind,
    Lifecycle,
    LocalAccountingVersion,
    Operation,
    Order,
    PortfolioBinding,
    Position,
    PositionState,
    Preview,
    Reservation,
    ReservationState,
    Side,
    Trader,
    TraderState,
    _runtime_lock,
)
from .ui_api.updates import record_operation

_submission_locks: dict[str, threading.RLock] = {}
_submission_locks_guard = threading.Lock()


def _submission_lock(key: str) -> threading.RLock:
    with _submission_locks_guard:
        return _submission_locks.setdefault(key, threading.RLock())


def serialized[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def call(*args: P.args, **kwargs: P.kwargs) -> R:
        service = cast("TraderService", args[0])
        service._require_context()
        assert service.prefix is not None
        with _submission_lock(service.prefix):
            return method(*args, **kwargs)

    return call


def _json(data: Any) -> str:
    return canonical_json(data)


class TraderService:
    """Authenticated trader-local service over broker and dbzero boundaries."""

    def __init__(
        self,
        authenticated_identity: str,
        config_profile: str = ".env",
        expected_environment: str | None = None,
        *,
        scope_verifier: ScopeVerifier | None = None,
        broker: BrokerAdapter | None = None,
        storage_root: Path | None = None,
        clock: Callable[[], datetime] = utc_now,
        barrier_hook: Callable[[str], None] | None = None,
    ) -> None:
        if not authenticated_identity:
            raise TraderError("TRADER_MISMATCH", "authenticated trader identity is required")
        self.trader_id = authenticated_identity
        self.profile: Profile = load_profile(config_profile)
        self._clock = clock
        self._barrier_hook = barrier_hook or (lambda _name: None)
        self._context = None
        self._direct_identity_source: str | None = None
        self._startup_error: TraderError | None = None
        self.broker = broker or EtoroBrokerAdapter(self.profile)
        try:
            if self.profile.trading_mode == "standalone":
                configured = route_environment(self.profile)
                if configured is None:
                    raise TraderError(
                        "ENVIRONMENT_UNVERIFIED", "standalone requires environment-specific broker routes"
                    )
                if expected_environment is not None and configured is not Environment(expected_environment):
                    raise TraderError(
                        "ENVIRONMENT_ASSERTION_MISMATCH", "expected environment differs from broker routes"
                    )
                identity = self.broker.probe_direct(configured)
                if not identity.get("account_id") or not identity.get("portfolio_id"):
                    raise TraderError("DIRECT_ACCOUNT_IDENTITY_UNVERIFIED", "broker account identity is unavailable")
                self._direct_identity_source = identity.get("identity_source", "broker_fields")
                self._context = VerifiedContext(
                    configured,
                    identity["account_id"],
                    identity["account_id"],
                    identity["portfolio_id"],
                    fingerprint(self.profile.user_key),
                    frozenset(),
                    clock(),
                    True,
                    bool(self.broker.capabilities().get("market")),
                )
            else:
                self._context = resolve_context(
                    self.profile,
                    scope_verifier or ControlScopeVerifier(),
                    expected_environment=expected_environment,
                    now=clock(),
                )
        except TraderError as exc:
            self._startup_error = exc
        self.store: DbzeroStore | None = None
        self.storage_key: str | None = None
        self.trader: Trader | None = None
        self.prefix: str | None = None
        with _runtime_lock:
            if self._context is not None:
                root = storage_root or fixed_storage_root(self._context.environment)
                self.store = DbzeroStore(root, self._context.environment)
                self.store.assert_prefix_isolation()
                credential = self.profile.service_credential
                if self.profile.trading_mode == "standalone" and credential is None:
                    credential = "direct:" + fingerprint(self.profile.user_key)
                if self.profile.trading_mode == "standalone":
                    try:
                        registration = self.store.authenticate(self.trader_id, credential)
                    except TraderError as exc:
                        if exc.code != "AUTHENTICATION_REQUIRED" or self.store.one_registration_exists(self.trader_id):
                            raise
                        assert credential is not None
                        registration = self.store.register(self.trader_id, credential)
                else:
                    registration = self.store.authenticate(self.trader_id, credential)
                self.trader = registration.trader
                self.storage_key = registration.storage_key
                self.prefix = self.store.trader_prefix(self.storage_key)
                if self.profile.trading_mode == "standalone":
                    self._validate_binding()

    @property
    def environment(self) -> str | None:
        return None if self._context is None else self._context.environment.value

    @property
    def trading_mode(self) -> str:
        return self.profile.trading_mode

    def _direct(self) -> DirectAccount:
        assert self.store is not None and self.prefix is not None and self._context is not None
        self.store.open(self.prefix)
        direct = DirectAccount()
        if direct.account_id and (
            direct.account_id != self._context.trading_account_id
            or direct.portfolio_id != self._context.trading_portfolio_id
            or direct.credential_fingerprint != self._context.credential_fingerprint
            or str(direct.environment).lower() != self.environment
        ):
            raise TraderError("ACCOUNT_MISMATCH", "direct account identity changed")
        return direct

    def _require_context(self) -> None:
        if self._startup_error is not None:
            raise self._startup_error
        if self._context is None or self.store is None or self.prefix is None or self.storage_key is None:
            raise TraderError("ENVIRONMENT_UNVERIFIED", "runtime context is not verified")

    def _objects(self) -> tuple[TraderState, PortfolioBinding]:
        self._require_context()
        assert self.store is not None and self.prefix is not None and self.trader is not None
        return self.store.state(self.prefix, self.trader), self.store.binding(self.prefix)

    def _validate_binding(
        self, *, active: bool = False, write: bool = False, reduction: bool = False
    ) -> tuple[TraderState, PortfolioBinding]:
        self._require_context()
        assert self._context is not None
        state, binding = self._objects()
        assert self.store is not None and self.prefix is not None
        if not LocalAccountingVersion().initialized:
            with self.store.transaction(self.prefix):
                initialize_accounting(state)
        if self.trading_mode == "standalone":
            assert self.store is not None and self.prefix is not None
            if binding.binding_version:
                raise TraderError("PORTFOLIO_SCOPE_MISMATCH", "standalone cannot reuse an agent portfolio")
            direct = self._direct()
            if not direct.account_id:
                with self.store.transaction(self.prefix):
                    direct.account_id = self._context.trading_account_id
                    direct.portfolio_id = self._context.trading_portfolio_id
                    direct.credential_fingerprint = self._context.credential_fingerprint
                    direct.identity_source = self._direct_identity_source or "broker_fields"
                    direct.environment = state.environment
                    direct.verified_at = self._clock()
                    direct.lifecycle = Lifecycle.READY
            if active and (not state.initialized or direct.lifecycle != Lifecycle.ACTIVE):
                raise TraderError("NOT_INITIALIZED", "initialize the standalone strategy first")
            if write and not self._context.can_write:
                raise TraderError("PERMISSION_REQUIRED", "broker opening capability is not configured")
            return state, binding
        if not binding.binding_version:
            raise TraderError("PORTFOLIO_NOT_BOUND", "no owner-provisioned portfolio is bound")
        if binding.trader != self.trader:
            raise TraderError("TRADER_MISMATCH", "binding belongs to a different trader")
        if binding.environment != state.environment:
            raise TraderError("PORTFOLIO_SCOPE_MISMATCH", "binding environment does not match token scopes")
        if (
            binding.agent_trading_account_id != self._context.trading_account_id
            or binding.agent_trading_portfolio_id != self._context.trading_portfolio_id
        ):
            raise TraderError("ACCOUNT_MISMATCH", "verified child identity does not match the binding")
        if binding.credential_fingerprint != self._context.credential_fingerprint:
            raise TraderError("PORTFOLIO_SCOPE_MISMATCH", "child credential rotation is not verified for this binding")
        lifecycle = binding.lifecycle
        if lifecycle == Lifecycle.RETIRED or (lifecycle == Lifecycle.SUSPENDED and not reduction):
            raise TraderError("PORTFOLIO_SUSPENDED", "portfolio is not permitted to accept operations")
        active_lifecycle = lifecycle == Lifecycle.ACTIVE or (reduction and lifecycle == Lifecycle.SUSPENDED)
        if active and (not state.initialized or not active_lifecycle):
            raise TraderError("NOT_INITIALIZED", "activate the existing portfolio binding first")
        if write and not self._context.can_write:
            raise TraderError("PERMISSION_REQUIRED", "the child token lacks its environment write scope")
        return state, binding

    def capabilities(self) -> dict[str, Any]:
        documented = {
            "market": True,
            "market_if_touched": True,
            "limit_ioc": True,
            "cancel": True,
            "partial_close": True,
            "modify_protection": True,
            "broker_notional_cap": "not_documented",
        }
        broker_caps = self.broker.capabilities()
        if self._context is None:
            return {
                "environment": None,
                "trading_mode": self.trading_mode,
                "direct_account_verified": False,
                "environment_verified": False,
                "verification_error": None if self._startup_error is None else self._startup_error.code,
                "documented": documented,
                "configured": broker_caps,
                "effective": {name: False for name in broker_caps},
            }
        effective = {name: bool(enabled and self._context.can_write) for name, enabled in broker_caps.items()}
        try:
            state, binding = self._validate_binding()
            ready = (
                (self._direct().lifecycle in {Lifecycle.READY, Lifecycle.ACTIVE})
                if self.trading_mode == "standalone"
                else bool(binding.copy_healthy and binding.lifecycle in {Lifecycle.READY, Lifecycle.ACTIVE})
            )
            effective = {name: value and ready for name, value in effective.items()}
            reduction_ready = (
                self._direct().lifecycle == Lifecycle.ACTIVE
                if self.trading_mode == "standalone"
                else binding.lifecycle in {Lifecycle.ACTIVE, Lifecycle.SUSPENDED}
            )
            for name in ("cancel", "cancel_close", "partial_close", "modify_protection", "request_lookup"):
                effective[name] = bool(broker_caps.get(name) and self._context.can_write and reduction_ready)
            lifecycle = self._direct().lifecycle if self.trading_mode == "standalone" else binding.lifecycle
            initialized = bool(state.initialized)
        except TraderError:
            lifecycle = Lifecycle.UNBOUND
            initialized = False
            ready = False
            effective = {name: False for name in effective}
        return {
            "environment": self.environment,
            "trading_mode": self.trading_mode,
            "direct_account_verified": self.trading_mode == "standalone" and ready,
            "readiness_error": None
            if ready
            else ("DIRECT_ACCOUNT_NOT_READY" if self.trading_mode == "standalone" else "PORTFOLIO_NOT_READY"),
            "environment_verified": True,
            "scope_read": self._context.can_read,
            "scope_write": self._context.can_write,
            "write_authorization": "broker_authoritative" if self.trading_mode == "standalone" else "verified_scope",
            "lifecycle": str(lifecycle),
            "initialized": initialized,
            "documented": documented,
            "configured": broker_caps,
            "effective": effective,
        }

    def trader_status(self) -> dict[str, Any]:
        if self._context is None:
            return {
                "trader_id": self.trader_id,
                "trading_mode": self.trading_mode,
                "direct_account_verified": False,
                "environment": None,
                "environment_verified": False,
                "lifecycle": str(Lifecycle.UNBOUND),
                "readiness_error": None if self._startup_error is None else self._startup_error.code,
            }
        state, binding = self._objects()
        strategy = Budget(
            money(state.strategy_initial_cap),
            money(state.strategy_realized, allow_negative=True),
            money(state.strategy_committed),
        )
        owner = Budget(
            money(state.owner_initial_cap),
            money(state.owner_realized, allow_negative=True),
            money(state.owner_committed),
        )
        return {
            "trader_id": self.trader_id,
            "trading_mode": self.trading_mode,
            "direct_account_verified": self.trading_mode == "standalone",
            "direct_account_id": self._direct().account_id if self.trading_mode == "standalone" else None,
            "direct_portfolio_id": self._direct().portfolio_id if self.trading_mode == "standalone" else None,
            "direct_identity_source": self._direct().identity_source if self.trading_mode == "standalone" else None,
            "environment": self.environment,
            "environment_verified": True,
            "lifecycle": str(self._direct().lifecycle if self.trading_mode == "standalone" else binding.lifecycle),
            "initialized": bool(state.initialized),
            "currency": str(state.currency),
            "owner_copy_investment_usd": str(binding.investment_usd),
            "strategy_virtual_balance_usd": str(
                state.strategy_initial_cap if self.trading_mode == "standalone" else binding.virtual_balance_usd
            ),
            "strategy_budget": strategy.as_dict(),
            "owner_mirror_budget": owner.as_dict(),
            "copy_healthy": bool(binding.copy_healthy) if self.trading_mode == "bound" else None,
            "mirror_reconciliation_state": "NOT_APPLICABLE" if self.trading_mode == "standalone" else "PENDING",
            "scope_status": (
                "broker_write_authoritative"
                if self.trading_mode == "standalone"
                else "read_write"
                if self._context.can_write
                else "read_only"
            ),
            "verified_at": api_value(
                self._direct().verified_at if self.trading_mode == "standalone" else binding.verified_at
            ),
        }

    def portfolio(self) -> dict[str, Any]:
        state, binding = self._validate_binding()
        if self.trading_mode == "standalone":
            return {
                "trader_id": self.trader_id,
                "trading_mode": "standalone",
                "environment": self.environment,
                "direct_account_id": self._direct().account_id,
                "direct_portfolio_id": self._direct().portfolio_id,
                "direct_identity_source": self._direct().identity_source,
                "lifecycle": str(self._direct().lifecycle),
                "policy_version": state.policy_version,
                "owner_copy_investment_usd": "0.00",
                "strategy_virtual_balance_usd": str(state.strategy_initial_cap),
                "copy_healthy": None,
                "mirror_reconciliation_state": "NOT_APPLICABLE",
            }
        return {
            "trader_id": self.trader_id,
            "environment": str(binding.environment).lower() if binding.environment is not None else None,
            "agent_portfolio_id": binding.agent_portfolio_id,
            "agent_portfolio_gcid": binding.agent_portfolio_gcid,
            "lifecycle": str(binding.lifecycle),
            "binding_version": binding.binding_version,
            "policy_version": state.policy_version,
            "owner_copy_investment_usd": str(binding.investment_usd),
            "strategy_virtual_balance_usd": str(binding.virtual_balance_usd),
            "copy_healthy": bool(binding.copy_healthy),
        }

    @serialized
    def initialize(self, expected_investment: object, currency: str = "USD") -> dict[str, Any]:
        self._require_context()
        assert self._context is not None
        self.broker.verify_identity(self._context)
        with _runtime_lock:
            state, binding = self._validate_binding()
            if currency != "USD":
                raise TraderError("UNSUPPORTED_CURRENCY", "only USD is supported")
            expected = money(expected_investment)
            if self.trading_mode == "standalone":
                if expected <= 0:
                    raise TraderError("INVALID_AMOUNT", "standalone capital must be positive")
                assert self.store is not None and self.prefix is not None
                if state.initialized:
                    if state.strategy_initial_cap == expected and state.currency == Currency.USD:
                        return self.trader_status()
                    raise TraderError("ALREADY_INITIALIZED", "strategy capital is immutable")
                direct = self._direct()
                if direct.lifecycle != Lifecycle.READY:
                    raise TraderError("DIRECT_ACCOUNT_NOT_READY", "direct broker identity is not ready")
                with self.store.transaction(self.prefix):
                    state.initialized = True
                    state.currency = Currency.USD
                    state.strategy_initial_cap = expected
                    state.owner_initial_cap = Decimal("0.00")
                    direct.lifecycle = Lifecycle.ACTIVE
                    self.store.append_audit(
                        self.prefix,
                        state,
                        kind="ACTIVATED",
                        actor=self.trader_id,
                        facts={"expected_investment_usd": str(expected), "trading_mode": "standalone"},
                    )
                return self.trader_status()
            if expected != money(binding.investment_usd):
                raise TraderError(
                    "ACCOUNT_MISMATCH", "expected investment does not match owner-approved copy investment"
                )
            if state.initialized:
                if state.owner_initial_cap == expected and state.currency == Currency.USD:
                    return self.trader_status()
                raise TraderError("ALREADY_INITIALIZED", "portfolio activation parameters are immutable")
            if not binding.copy_healthy or binding.lifecycle != Lifecycle.READY:
                raise TraderError("PORTFOLIO_NOT_READY", "portfolio identity and copy relationship must be ready")
            assert (
                self.broker is not None
                and self._context is not None
                and self.store is not None
                and self.prefix is not None
            )
            with self.store.transaction(self.prefix):
                state.initialized = True
                state.currency = Currency.USD
                state.owner_initial_cap = expected
                state.strategy_initial_cap = min(expected, money(binding.virtual_balance_usd))
                binding.lifecycle = Lifecycle.ACTIVE
                self.store.append_audit(
                    self.prefix,
                    state,
                    kind="ACTIVATED",
                    actor=self.trader_id,
                    facts={"expected_investment_usd": str(expected), "currency": currency},
                )
            return self.trader_status()

    def _budget_pair(self, state: TraderState) -> tuple[Budget, Budget]:
        return (
            Budget(
                money(state.strategy_initial_cap),
                money(state.strategy_realized, allow_negative=True),
                money(state.strategy_committed),
            ),
            Budget(
                money(state.owner_initial_cap),
                money(state.owner_realized, allow_negative=True),
                money(state.owner_committed),
            ),
        )

    def _persist_preview(
        self,
        operation: Operation,
        params: dict[str, Any],
        state: TraderState,
        binding: PortfolioBinding,
        fingerprint: str,
    ) -> dict[str, Any]:
        assert self.store is not None and self.prefix is not None
        created = storage_datetime(self._clock())
        expires = created + timedelta(minutes=5)
        self.store.open(self.prefix)
        preview = Preview(
            operation,
            native_copy(params),
            created,
            expires,
            fingerprint,
            int(binding.binding_version),
            int(state.policy_version),
        )
        self.store.tag(preview, "PREVIEW")
        self.store.commit(self.prefix)
        return {
            "preview_id": str(db0.uuid(preview)),
            "operation": str(operation),
            "created_at": created.isoformat(),
            "expires_at": expires.isoformat(),
            "binding_version": binding.binding_version,
            "policy_version": state.policy_version,
            "broker_state_fingerprint": fingerprint,
            **api_value(params),
        }

    @serialized
    def preview_open(
        self,
        *,
        symbol: str | None = None,
        # External eToro numeric instrument catalogue ID, not a memo ID.
        instrument_id: int | None = None,
        side: str,
        order_type: str = "market",
        strategy_notional_usd: object,
        leverage: int = 1,
        trigger_rate: object | None = None,
        limit_rate: object | None = None,
        stop_loss_rate: object | None = None,
        take_profit_rate: object | None = None,
    ) -> dict[str, Any]:
        with _runtime_lock:
            state, binding = self._validate_binding(active=True, write=True)
            if self.trading_mode == "bound" and not binding.copy_healthy:
                raise TraderError("COPY_DIVERGENCE", "owner mirror must be reconciled before new exposure")
            if not self.broker.capabilities().get(order_type, False):
                raise TraderError("UNSUPPORTED_CAPABILITY", "opening order type is not effectively enabled")
            assert self._context is not None
            notional = money(strategy_notional_usd)
        sizing = self.broker.resolve_sizing(
            context=self._context,
            symbol=symbol,
            instrument_id=instrument_id,
            strategy_notional_usd=notional,
            leverage=leverage,
            order_type=order_type,
            side=side,
        )
        strategy_cost = self.broker.estimate_costs(context=self._context, sizing=sizing, side=side)
        fingerprint = self.broker.state_fingerprint(self._context)
        with _runtime_lock:
            state, binding = self._validate_binding(active=True, write=True)
            strategy_budget, owner_budget = self._budget_pair(state)
            if self.trading_mode == "standalone":
                reservation = reserve_money(sizing.full_notional_usd + strategy_cost)
                if reservation > strategy_budget.available_to_open:
                    raise TraderError("INSUFFICIENT_BUDGET", "the operation exceeds the strategy budget")
                from .accounting import Admission

                admission = Admission(
                    reservation,
                    Decimal("0.00"),
                    money(strategy_budget.available_to_open - reservation),
                    Decimal("0.00"),
                    Decimal("0.00"),
                )
                owner_cost = Decimal("0.00")
            else:
                ratio = money(binding.investment_usd) / money(binding.virtual_balance_usd)
                owner_cost = money(strategy_cost * ratio)
                admission = admit_open(
                    strategy=strategy_budget,
                    owner=owner_budget,
                    strategy_notional=sizing.full_notional_usd,
                    strategy_cost_buffer=strategy_cost,
                    owner_cost_buffer=owner_cost,
                    investment=money(binding.investment_usd),
                    virtual_balance=money(binding.virtual_balance_usd),
                )

            def convert(value: object | None) -> Decimal | None:
                return None if value is None else money(value)

            payload = open_payload(
                sizing=sizing,
                side=side,
                order_type=order_type,
                leverage=leverage,
                trigger_rate=convert(trigger_rate),
                limit_rate=convert(limit_rate),
                stop_loss_rate=convert(stop_loss_rate),
                take_profit_rate=convert(take_profit_rate),
            )
            params = {
                "symbol": symbol,
                "instrument_id": sizing.instrument_id,
                "side": side,
                "order_type": order_type,
                "strategy_notional_usd": sizing.full_notional_usd,
                "broker_amount_usd": sizing.amount_usd,
                "broker_units": sizing.units,
                "leverage": leverage,
                "estimated_strategy_cost_usd": strategy_cost,
                "estimated_owner_copied_notional_usd": admission.estimated_owner_notional,
                "estimated_owner_cost_usd": owner_cost,
                "strategy_reservation_usd": admission.strategy_reservation,
                "owner_reservation_usd": admission.owner_reservation,
                "strategy_remaining_budget_usd": admission.strategy_remaining,
                "owner_remaining_budget_usd": admission.owner_remaining,
                "broker_payload": payload,
                "trigger_rate": convert(trigger_rate),
                "limit_rate": convert(limit_rate),
                "stop_loss_rate": convert(stop_loss_rate),
                "take_profit_rate": convert(take_profit_rate),
            }
            return self._persist_preview(Operation.open, params, state, binding, fingerprint)

    def _owned_position(self, position_id: str) -> Position:
        assert self.store is not None and self.prefix is not None
        position = self.store.one(Position, position_id, prefix=self.prefix)
        if position is None or position.state != PositionState.OPEN:
            raise TraderError("NOT_FOUND", "position not found")
        return position

    def _owned_order(self, order_id: str) -> Order:
        assert self.store is not None and self.prefix is not None
        order = self.store.one(Order, order_id, prefix=self.prefix)
        if order is None or order.state not in {ExecutionState.PENDING, ExecutionState.ACKNOWLEDGED}:
            raise TraderError("NOT_FOUND", "order not found")
        return order

    @serialized
    def preview_close(
        self,
        *,
        # External eToro numeric position ID represented as text, not a memo ID.
        position_id: str,
        fraction: object = "1",
    ) -> dict[str, Any]:
        self._require_context()
        assert self._context is not None
        fingerprint = self.broker.state_fingerprint(self._context)
        with _runtime_lock:
            state, binding = self._validate_binding(active=True, write=True, reduction=True)
            position = self._owned_position(position_id)
            close_fraction = Decimal(str(fraction))
            if not close_fraction.is_finite() or close_fraction <= 0 or close_fraction > 1:
                raise TraderError("INVALID_AMOUNT", "fraction must be in (0, 1]")
            if not self.broker.capabilities().get("partial_close"):
                raise TraderError("UNSUPPORTED_CAPABILITY", "position closing is unavailable")
            units = position.units * close_fraction
            params = {
                "position_id": position_id,
                "fraction": close_fraction,
                "units_to_deduct": units,
                "instrument_id": int(position.instrument_id),
                "symbol": position.symbol,
                "strategy_notional_usd": money(position.strategy_notional_usd * close_fraction),
            }
            assert self._context is not None
            return self._persist_preview(Operation.close, params, state, binding, fingerprint)

    @serialized
    def preview_modify(
        self,
        *,
        # External eToro numeric position ID represented as text, not a memo ID.
        position_id: str,
        stop_loss_rate: object | None = None,
        take_profit_rate: object | None = None,
        stop_loss_type: str | None = None,
    ) -> dict[str, Any]:
        self._require_context()
        assert self._context is not None
        fingerprint = self.broker.state_fingerprint(self._context)
        with _runtime_lock:
            state, binding = self._validate_binding(active=True, write=True, reduction=True)
            self._owned_position(position_id)
            if stop_loss_rate is None and take_profit_rate is None:
                raise TraderError("INVALID_ORDER", "at least one protection rate is required")
            if stop_loss_type not in {None, "rate", "trailing"}:
                raise TraderError("INVALID_ORDER", "unsupported stop-loss type")
            params = {
                "position_id": position_id,
                "stop_loss_rate": None if stop_loss_rate is None else money(stop_loss_rate),
                "take_profit_rate": None if take_profit_rate is None else money(take_profit_rate),
                "stop_loss_type": stop_loss_type,
            }
            assert self._context is not None
            return self._persist_preview(Operation.modify, params, state, binding, fingerprint)

    @serialized
    def preview_cancel(
        self,
        *,
        # External eToro numeric order ID represented as text, not a memo ID.
        order_id: str,
    ) -> dict[str, Any]:
        self._require_context()
        assert self._context is not None
        fingerprint = self.broker.state_fingerprint(self._context)
        with _runtime_lock:
            state, binding = self._validate_binding(active=True, write=True, reduction=True)
            order = self._owned_order(order_id)
            cancel_type = "cancel_close" if order.intent.operation == Operation.close else "cancel"
            if not self.broker.capabilities().get(cancel_type):
                raise TraderError("UNSUPPORTED_CAPABILITY", "pending order cancellation is unavailable")
            assert self._context is not None
            return self._persist_preview(
                Operation.cancel,
                {
                    "order_id": order_id,
                    "symbol": order.symbol,
                    "instrument_id": order.intent.params.get("instrument_id"),
                    "cancel_type": cancel_type,
                },
                state,
                binding,
                fingerprint,
            )

    def _api_reference[T](self, identifier: str, model: type[T]) -> T:
        """Resolve an incoming API ID once, checking both type and trader ownership."""
        assert self.prefix is not None
        try:
            item = db0.fetch(identifier, model)
        except (RuntimeError, ValueError, TypeError) as exc:
            raise TraderError("NOT_FOUND", f"{model.__name__.lower()} not found") from exc
        # fetch's prefix argument only scopes singleton lookups, not UUID lookups.
        if db0.get_prefix_of(item).name != self.prefix.lstrip("/"):
            raise TraderError("NOT_FOUND", f"{model.__name__.lower()} not found")
        return cast(T, item)

    def _validate_preview(self, preview: Preview) -> None:
        if preview.expires_at.astimezone(UTC) <= self._clock():
            raise TraderError("STALE_PREVIEW", "preview expired")

    @serialized
    def submit(
        self,
        # Preview's dbzero UUID serialized at the API boundary; internal links use instances.
        preview_id: str,
        # Caller-supplied local submission deduplication key, not a broker identifier.
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise TraderError("INVALID_IDEMPOTENCY_KEY", "a bounded idempotency key is required")
        with _runtime_lock:
            state, binding = self._validate_binding(active=True, write=True, reduction=True)
            assert (
                self.store is not None
                and self.prefix is not None
                and self.storage_key is not None
                and self._context is not None
            )
            key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
            key_tag = f"key:{'direct' if self.trading_mode == 'standalone' else binding.binding_version}:{key_digest}"
        with _submission_lock(self.prefix):
            with _runtime_lock:
                preview = self._api_reference(preview_id, Preview)
                prior = self.store.one(Intent, key_tag, prefix=self.prefix)
                if prior is not None:
                    if prior.preview != preview:
                        raise TraderError("IDEMPOTENCY_CONFLICT", "idempotency key was used for another preview")
                    return self._intent_value(prior)
                self._validate_preview(preview)
                state, binding = self._validate_binding(
                    active=True, write=True, reduction=preview.operation != Operation.open
                )
                if preview.binding_version != binding.binding_version or preview.policy_version != state.policy_version:
                    raise TraderError("STALE_PREVIEW", "binding or policy changed")
            broker_fingerprint = self.broker.state_fingerprint(self._context)
            with _runtime_lock:
                state, binding = self._validate_binding(
                    active=True, write=True, reduction=preview.operation != Operation.open
                )
                self._validate_preview(preview)
                if preview.binding_version != binding.binding_version or preview.policy_version != state.policy_version:
                    raise TraderError("STALE_PREVIEW", "binding or policy changed")
                if preview.state_fingerprint != broker_fingerprint:
                    raise TraderError("STALE_PREVIEW", "broker state changed")
                params = native_copy(preview.params)
                self._revalidate_admission(preview.operation, params, state, binding)
                request_id = str(uuid.uuid4())
                command_digest = hashlib.sha256(
                    _json(
                        [
                            self.environment,
                            self.trader_id,
                            binding.binding_version,
                            preview.operation,
                            params,
                            request_id,
                        ]
                    ).encode()
                ).hexdigest()
                strategy_reservation = money(params.get("strategy_reservation_usd", "0"))
                owner_reservation = money(params.get("owner_reservation_usd", "0"))
                self.store.open(self.prefix)
                with self.store.transaction(self.prefix):
                    intent = Intent(
                        preview,
                        idempotency_key,
                        preview.operation,
                        native_copy(preview.params),
                        request_id,
                        command_digest,
                        int(binding.binding_version),
                        int(state.policy_version),
                        created_at=storage_datetime(self._clock()),
                    )
                    self.store.tag(intent, key_tag, "INTENT")
                    if preview.operation in {Operation.close, Operation.modify}:
                        db0.tags(self._owned_position(params["position_id"])).add(db0.as_tag(intent))
                    elif preview.operation == Operation.cancel:
                        db0.tags(self._owned_order(params["order_id"])).add(db0.as_tag(intent))
                    reservation = Reservation(
                        intent,
                        strategy_reservation,
                        owner_reservation,
                        int(binding.binding_version),
                        int(state.policy_version),
                    )
                    self.store.tag(reservation, "RESERVATION")
                    state.strategy_committed = money(state.strategy_committed) + strategy_reservation
                    state.owner_committed = money(state.owner_committed) + owner_reservation
                    self.store.append_audit(
                        self.prefix,
                        state,
                        kind="INTENT_COMMITTED",
                        actor=self.trader_id,
                        intent=intent,
                        facts={"operation": str(preview.operation), "request_id": request_id},
                    )
                self._barrier_hook("trader_intent_committed")

                control = self.store.reserve_control(
                    command_digest=command_digest,
                    storage_key=self.storage_key,
                    request_id=request_id,
                    binding_version=int(binding.binding_version),
                )
                with self.store.transaction(self.prefix):
                    self.store.open(self.prefix)
                    intent.state = ExecutionState.ADMITTED
                    record_operation(intent, self._clock())
                self._barrier_hook("control_reservation_committed")

                mutation = self._mutation(preview.operation, params, request_id)
            error_code = None
            try:
                outcome = self.broker.dispatch(self._context, mutation)
            except TraderError as exc:
                outcome = BrokerOutcome(
                    state=IntentState.UNKNOWN if exc.code == "BROKER_OUTCOME_UNKNOWN" else IntentState.REJECTED,
                    request_id=request_id,
                )
                error_code = exc.code
            except Exception:
                outcome = BrokerOutcome(IntentState.UNKNOWN, request_id)
                error_code = "BROKER_OUTCOME_UNKNOWN"
            with _runtime_lock:
                self.store.open(self.prefix)
                intent.error_code = error_code
            with _runtime_lock:
                for entity_type, broker_id in (
                    ("order", outcome.broker_order_id),
                    ("position", outcome.broker_position_id),
                ):
                    if broker_id is not None:
                        self.store.claim_ownership(
                            environment=self._context.environment.value,
                            trading_account_id=self._context.trading_account_id,
                            trading_portfolio_id=self._context.trading_portfolio_id,
                            entity_type=entity_type,
                            broker_id=broker_id,
                            storage_key=self.storage_key,
                        )
                control.state = getattr(ExecutionState, outcome.state.name)
                control.outcome = {**asdict(outcome), "state": outcome.state.value}
                self.store.append_control_event(
                    "BROKER_OUTCOME_RECORDED",
                    {
                        "command_digest": command_digest,
                        "request_id": request_id,
                        "state": outcome.state.value,
                    },
                )
                self.store.commit(self.store.control_prefix)
                self._barrier_hook("control_outcome_committed")
                self._project_outcome(intent, reservation, outcome, params, state, binding)
                self.store.commit(self.prefix)
                self._barrier_hook("trader_outcome_projected")
                return self._intent_value(intent)

    def _revalidate_admission(
        self, operation: Operation, params: dict[str, Any], state: TraderState, binding: PortfolioBinding
    ) -> None:
        if operation in {Operation.close, Operation.modify}:
            position = self._owned_position(params["position_id"])
            if operation == Operation.close:
                if position.units <= 0:
                    raise TraderError("EXECUTION_DETAILS_UNAVAILABLE", "confirmed position units are required")
                if position.units * Decimal(params["fraction"]) != Decimal(params["units_to_deduct"]):
                    raise TraderError("STALE_PREVIEW", "position size changed")
            pending = self.store.all(Intent, prefix=self.prefix) if self.store and self.prefix else []
            if any(
                i.operation == Operation.close
                and i.params.get("position_id") == position.position_id
                and i.state in {ExecutionState.ADMITTED, ExecutionState.UNKNOWN, ExecutionState.ACKNOWLEDGED}
                for i in pending
            ):
                raise TraderError("EXECUTION_PENDING", "reconcile the existing close first")
            return
        if operation == Operation.cancel:
            self._owned_order(params["order_id"])
            assert self.store is not None and self.prefix is not None
            if any(
                i.operation == Operation.cancel
                and i.params.get("order_id") == params["order_id"]
                and i.state in {ExecutionState.ADMITTED, ExecutionState.UNKNOWN, ExecutionState.ACKNOWLEDGED}
                for i in self.store.all(Intent, prefix=self.prefix)
            ):
                raise TraderError("EXECUTION_PENDING", "reconcile the existing cancellation first")
            return
        if self.trading_mode == "bound" and not binding.copy_healthy:
            raise TraderError("COPY_DIVERGENCE", "owner mirror must be reconciled before new exposure")
        strategy, owner = self._budget_pair(state)
        if money(params["strategy_reservation_usd"]) > strategy.available_to_open:
            raise TraderError("INSUFFICIENT_BUDGET", "strategy budget changed")
        if self.trading_mode == "bound" and money(params["owner_reservation_usd"]) > owner.available_to_open:
            raise TraderError("INSUFFICIENT_BUDGET", "owner budget changed")

    def _mutation(self, operation: Operation, params: dict[str, Any], request_id: str) -> BrokerMutation:
        if operation == Operation.open:
            return BrokerMutation(request_id, str(operation), api_value(params["broker_payload"]))
        if operation == Operation.close:
            payload: dict[str, Any] = {"InstrumentID": params["instrument_id"]}
            if Decimal(params["fraction"]) < 1:
                payload["UnitsToDeduct"] = params["units_to_deduct"]
            return BrokerMutation(
                request_id,
                str(operation),
                api_value(payload),
                params["position_id"],
            )
        if operation == Operation.modify:
            payload = {
                key: value
                for key, value in {
                    "stopLossRate": params["stop_loss_rate"],
                    "takeProfitRate": params["take_profit_rate"],
                    "stopLossType": params["stop_loss_type"],
                }.items()
                if value is not None
            }
            return BrokerMutation(request_id, str(operation), api_value(payload), params["position_id"])
        if operation == Operation.cancel:
            return BrokerMutation(request_id, params.get("cancel_type", "cancel"), {}, params["order_id"])
        raise TraderError("UNSUPPORTED_CAPABILITY", "unknown preview operation")

    def _project_outcome(
        self,
        intent: Intent,
        reservation: Reservation,
        outcome: BrokerOutcome,
        params: dict[str, Any],
        state: TraderState,
        binding: PortfolioBinding,
    ) -> None:
        assert self.store is not None and self.prefix is not None
        with _runtime_lock:
            self.store.open(self.prefix)
            with db0.atomic():
                target_state = getattr(ExecutionState, outcome.state.name)
                if (
                    intent.projected_state in {ExecutionState.FILLED, ExecutionState.REJECTED, ExecutionState.CANCELED}
                    and intent.projected_state != target_state
                ):
                    return
                missing_position = (
                    intent.operation == Operation.open
                    and outcome.state == IntentState.FILLED
                    and not intent.broker_position_id
                    and outcome.broker_position_id is not None
                )
                if intent.projected_state != target_state or missing_position:
                    self._apply_outcome(intent, reservation, outcome, params, state, binding)
                    intent.projected_state = target_state
                record_fill(intent, outcome, state)
                self._sync_position(intent, state, binding)
                record_operation(intent, self._clock(), outcome)
            self.store.commit(self.prefix)

    def _sync_position(self, intent: Intent, state: TraderState, binding: PortfolioBinding) -> None:
        assert self.store is not None and self.prefix is not None
        position_id = (
            intent.broker_position_id if intent.operation == Operation.open else intent.params.get("position_id")
        )
        if not position_id or intent.operation not in {Operation.open, Operation.close}:
            return
        position = self.store.one(Position, str(position_id), prefix=self.prefix)
        if position is None:
            return
        entry = fill_for(position.intent)
        value = position_value(position)
        if entry is None or not entry.units or value.remaining_units is None:
            return
        remaining_notional = money(
            Decimal(position.intent.params["strategy_notional_usd"]) * value.remaining_units / entry.units
        )
        released = position.strategy_notional_usd - remaining_notional
        state.strategy_committed = max(Decimal(0), state.strategy_committed - released)
        if self.trading_mode == "bound":
            owner_delta = released * binding.investment_usd / binding.virtual_balance_usd
            state.owner_committed = max(Decimal(0), money(state.owner_committed - owner_delta))
        position.units = value.remaining_units
        position.strategy_notional_usd = remaining_notional
        position.state = PositionState.CLOSED if value.remaining_units == 0 else PositionState.OPEN

    def _apply_outcome(
        self,
        intent: Intent,
        reservation: Reservation,
        outcome: BrokerOutcome,
        params: dict[str, Any],
        state: TraderState,
        binding: PortfolioBinding,
    ) -> None:
        assert self.store is not None and self.prefix is not None
        self.store.open(self.prefix)
        intent.state = getattr(ExecutionState, outcome.state.name)
        intent.broker_order_id = outcome.broker_order_id or intent.broker_order_id
        intent.broker_position_id = outcome.broker_position_id or intent.broker_position_id
        if outcome.state in {IntentState.REJECTED, IntentState.CANCELED} and reservation.state == ReservationState.HELD:
            state.strategy_committed = max(
                Decimal("0"), money(state.strategy_committed) - money(reservation.strategy_amount_usd)
            )

            state.owner_committed = max(
                Decimal("0"), money(state.owner_committed) - money(reservation.owner_amount_usd)
            )

            reservation.state = ReservationState.RELEASED
        if outcome.broker_order_id and intent.operation in {Operation.open, Operation.close}:
            order = self.store.one(Order, outcome.broker_order_id, prefix=self.prefix)
            order_state = (
                ExecutionState.PENDING
                if outcome.state is IntentState.ACKNOWLEDGED
                else getattr(ExecutionState, outcome.state.name)
            )
            if order is None:
                symbol = params.get("symbol") or ""
                if intent.operation == Operation.close:
                    symbol = self._owned_position(params["position_id"]).symbol
                order = Order(
                    outcome.broker_order_id,
                    intent,
                    symbol,
                    order_state,
                )
                self.store.tag(order, "ORDER")
            else:
                order.state = order_state
        if intent.operation == Operation.open and outcome.state is IntentState.FILLED and outcome.broker_position_id:
            position = Position(
                outcome.broker_position_id,
                intent,
                params.get("symbol") or "",
                getattr(Side, params["side"]),
                int(params["instrument_id"]),
                int(params["leverage"]),
                money(params["strategy_notional_usd"]),
                outcome.filled_units if outcome.filled_units is not None else Decimal(0),
            )
            self.store.tag(position, "POSITION")
            entry = LedgerEntry(
                LedgerDomain.strategy,
                LedgerKind.ACTUAL_FILL,
                money(params["strategy_notional_usd"]),
                intent,
                self._clock(),
            )
            self.store.tag(entry, "LEDGER")
            if self.trading_mode == "bound":
                binding.copy_healthy = False
        elif intent.operation == Operation.close and outcome.state is IntentState.FILLED:
            position = self._owned_position(params["position_id"])
            db0.tags(position).add(db0.as_tag(intent))
            if self.trading_mode == "bound":
                binding.copy_healthy = False
        elif intent.operation == Operation.cancel and outcome.state in {IntentState.FILLED, IntentState.CANCELED}:
            order = self._owned_order(params["order_id"])
            db0.tags(order).add(db0.as_tag(intent))
            order.state = ExecutionState.CANCELED
            original_reservation = self.store.one(Reservation, db0.as_tag(order.intent), prefix=self.prefix)
            if original_reservation is not None and original_reservation.state == ReservationState.HELD:
                state.strategy_committed = max(
                    Decimal("0"),
                    money(state.strategy_committed) - money(original_reservation.strategy_amount_usd),
                )

                state.owner_committed = max(
                    Decimal("0"),
                    money(state.owner_committed) - money(original_reservation.owner_amount_usd),
                )

                original_reservation.state = ReservationState.RELEASED
        elif intent.operation == Operation.modify and outcome.state in {IntentState.FILLED, IntentState.ACKNOWLEDGED}:
            position = self._owned_position(params["position_id"])
            db0.tags(position).add(db0.as_tag(intent))
            if params["stop_loss_rate"] is not None:
                position.stop_loss_rate = Decimal(params["stop_loss_rate"])
            if params["take_profit_rate"] is not None:
                position.take_profit_rate = Decimal(params["take_profit_rate"])
        self.store.append_audit(
            self.prefix,
            state,
            kind=f"BROKER_{outcome.state.value}",
            actor="coordinator",
            intent=intent,
            facts={
                "operation": str(intent.operation),
                "request_id": outcome.request_id,
                "order_id": outcome.broker_order_id,
                "position_id": outcome.broker_position_id,
                "response_fingerprint": outcome.raw_fingerprint,
                "mirror_reconciliation_state": "NOT_APPLICABLE"
                if self.trading_mode == "standalone"
                else ("PENDING" if outcome.state is IntentState.FILLED else "UNCHANGED"),
            },
        )

    def _intent_value(self, intent: Intent) -> dict[str, Any]:
        return {
            "intent_id": str(db0.uuid(intent)),
            "operation": str(intent.operation),
            "state": str(intent.state),
            "request_id": intent.request_id,
            "broker_order_id": intent.broker_order_id or None,
            "broker_position_id": intent.broker_position_id or None,
            "strategy_execution_state": str(intent.state),
            "mirror_reconciliation_state": "NOT_APPLICABLE"
            if self.trading_mode == "standalone"
            else ("PENDING" if intent.state == ExecutionState.FILLED else "UNCHANGED"),
            "error_code": intent.error_code or None,
        }

    def smoke_demo(
        self,
        expected_investment: object,
        symbol: str = "ETH",
        strategy_notional_usd: object = "100",
        *,
        reconcile: bool = False,
    ) -> dict[str, Any]:
        """One durable direct-demo order per account, trader and exact smoke parameters."""
        if self.trading_mode != "standalone" or self.environment != "demo":
            raise TraderError("DEMO_ONLY", "smoke trading requires an explicit standalone demo profile")
        self._require_context()
        assert self.store is not None and self.prefix is not None and self._context is not None
        capital = money(expected_investment)
        notional = money(strategy_notional_usd)
        if not symbol or not symbol.isalnum() or notional <= 0:
            raise TraderError("INVALID_ORDER", "smoke symbol and positive notional are required")
        key = (
            "smoke-demo:"
            + hashlib.sha256(
                _json(
                    [
                        self.trader_id,
                        self._context.trading_account_id,
                        capital,
                        symbol,
                        notional,
                    ]
                ).encode()
            ).hexdigest()
        )
        tag = "key:direct:" + hashlib.sha256(key.encode()).hexdigest()
        with _submission_lock(self.prefix):
            self.initialize(capital)
            prior = self.store.one(Intent, tag, prefix=self.prefix)
            if prior is None:
                if reconcile:
                    raise TraderError("NOT_FOUND", "no smoke submission exists to reconcile")
                preview = self.preview_open(symbol=symbol, side="long", strategy_notional_usd=notional)
                outcome = self.submit(preview["preview_id"], key)
            else:
                preview = None
                outcome = self._intent_value(prior)
            reconciliation = self.reconcile() if reconcile else None
            if reconcile:
                prior = self.store.one(Intent, tag, prefix=self.prefix)
                assert prior is not None
                outcome = self._intent_value(prior)
            return {
                "trading_mode": "standalone",
                "symbol": symbol,
                "strategy_notional_usd": str(notional),
                "strategy_initial_cap_usd": str(capital),
                "preview": preview,
                "outcome": outcome,
                "reconciliation": reconciliation,
                "reconcile_command": "smoke-demo --expected-investment "
                + str(capital)
                + " --symbol "
                + symbol
                + " --strategy-notional-usd "
                + str(notional)
                + " --reconcile"
                if outcome["state"] in {"UNKNOWN", "ACKNOWLEDGED", "ADMITTED"}
                else None,
            }

    def intent_status(
        self,
        # Intent's dbzero UUID serialized at the API boundary; internal links use instances.
        intent_id: str,
    ) -> dict[str, Any]:
        self._validate_binding(active=True)
        assert self.store is not None and self.prefix is not None
        intent = self._api_reference(intent_id, Intent)
        return self._intent_value(intent)

    def positions(self) -> list[dict[str, Any]]:
        self._validate_binding(active=True)
        assert self.store is not None and self.prefix is not None
        return [
            {
                "position_id": item.position_id,
                "symbol": item.symbol,
                "side": str(item.side),
                "instrument_id": item.instrument_id,
                "leverage": item.leverage,
                "strategy_notional_usd": str(item.strategy_notional_usd),
                "units": str(item.units),
                "stop_loss_rate": str(item.stop_loss_rate) if item.stop_loss_rate is not None else None,
                "take_profit_rate": str(item.take_profit_rate) if item.take_profit_rate is not None else None,
                "state": str(item.state),
            }
            for item in self.store.all(Position, "POSITION", prefix=self.prefix)
            if item.state == PositionState.OPEN
        ]

    def orders(self) -> list[dict[str, Any]]:
        self._validate_binding(active=True)
        assert self.store is not None and self.prefix is not None
        return [
            {
                "order_id": item.order_id,
                "symbol": item.symbol,
                "state": str(item.state),
                "intent_id": str(db0.uuid(item.intent)),
            }
            for item in self.store.all(Order, "ORDER", prefix=self.prefix)
            if item.state in {ExecutionState.PENDING, ExecutionState.ACKNOWLEDGED}
        ]

    @serialized
    def refresh_execution_details(self) -> int:
        """Look up only locally recorded executions; never import broker positions."""
        with _runtime_lock:
            state, binding = self._validate_binding(active=True)
            assert self.store is not None and self.prefix is not None and self._context is not None
            candidates = []
            for intent in self.store.all(Intent, prefix=self.prefix):
                if intent.operation not in {Operation.open, Operation.close}:
                    continue
                fill = fill_for(intent)
                if intent.state == ExecutionState.FILLED and (
                    (intent.operation == Operation.open and not intent.broker_position_id)
                    or fill is None
                    or fill.units is None
                    or fill.price is None
                    or fill.costs is None
                    or (intent.operation == Operation.close and fill.gross_pnl is None)
                ):
                    candidates.append((intent, intent.broker_order_id, intent.operation, str(intent.request_id)))
        updated = 0
        for intent, order_id, operation, request_id in candidates:
            lookup = self.broker.lookup_close_order if operation == Operation.close else self.broker.lookup_order
            outcome = (
                lookup(self._context, str(order_id))
                if order_id
                else self.broker.lookup_request(self._context, request_id)
            )
            if outcome is None or outcome.state != IntentState.FILLED:
                continue
            with _runtime_lock:
                self.store.open(self.prefix)
                if (order_id and outcome.broker_order_id != order_id) or (
                    not order_id and outcome.request_id != request_id
                ):
                    raise TraderError("ACCOUNT_MISMATCH", "execution order does not match local ownership")
                if (
                    operation == Operation.open
                    and outcome.broker_position_id is not None
                    and intent.broker_position_id is not None
                    and outcome.broker_position_id != intent.broker_position_id
                ):
                    raise TraderError("ACCOUNT_MISMATCH", "execution position does not match local ownership")
                if operation == Operation.open and outcome.broker_position_id:
                    assert self.storage_key is not None
                    self.store.claim_ownership(
                        environment=self._context.environment.value,
                        trading_account_id=self._context.trading_account_id,
                        trading_portfolio_id=self._context.trading_portfolio_id,
                        entity_type="position",
                        broker_id=outcome.broker_position_id,
                        storage_key=self.storage_key,
                    )
                reservation = self.store.one(Reservation, db0.as_tag(intent), prefix=self.prefix)
                if reservation is not None:
                    self._project_outcome(intent, reservation, outcome, native_copy(intent.params), state, binding)
                    for position in self.store.all(Position, prefix=self.prefix):
                        value = position_value(position)
                        if value.remaining_units is not None:
                            position.units = value.remaining_units
                    self.store.commit(self.prefix)
                    updated += 1
        return updated

    @serialized
    def reconcile(self) -> dict[str, Any]:
        with _runtime_lock:
            state, binding = self._validate_binding(active=True)
            assert (
                self._context is not None
                and self.store is not None
                and self.prefix is not None
                and self.storage_key is not None
            )
            recovered = 0
            unresolved = 0
            controls = list(self.store.unresolved_control(self.storage_key))
            control_digests = {str(control.command_digest) for control in controls}
            for local_intent in self.store.all(Intent, "INTENT", prefix=self.prefix):
                if (
                    local_intent.state == ExecutionState.COMMITTED
                    and local_intent.command_digest not in control_digests
                ):
                    with self.store.transaction(self.prefix):
                        reservation = self.store.one(Reservation, db0.as_tag(local_intent), prefix=self.prefix)
                        if reservation is not None and reservation.state == ReservationState.HELD:
                            state.strategy_committed = max(
                                Decimal(0), state.strategy_committed - reservation.strategy_amount_usd
                            )
                            state.owner_committed = max(
                                Decimal(0), state.owner_committed - reservation.owner_amount_usd
                            )
                            reservation.state = ReservationState.RELEASED
                        local_intent.state = ExecutionState.CANCELED
                        self.store.append_audit(
                            self.prefix,
                            state,
                            kind="ORPHAN_INTENT_CANCELED",
                            actor="coordinator",
                            intent=local_intent,
                            source="reconcile",
                        )
                    recovered += 1
        for control in controls:
            with _runtime_lock:
                intent = self.store.one(Intent, control.request_id, prefix=self.prefix)
                if intent is None:
                    continue
                if (
                    intent.state in {ExecutionState.FILLED, ExecutionState.REJECTED, ExecutionState.CANCELED}
                    and intent.projected_state == intent.state
                ):
                    # A saved initial outcome must not overwrite newer confirmed accounting.
                    continue
                control_state = control.state
                request_id = str(control.request_id)
                order_id = intent.broker_order_id
                operation = intent.operation
                saved = native_copy(control.outcome)
            if control_state in {ExecutionState.RESERVED, ExecutionState.UNKNOWN}:
                outcome = self.broker.lookup_request(self._context, request_id)
            elif control_state == ExecutionState.ACKNOWLEDGED and order_id:
                lookup = self.broker.lookup_close_order if operation == Operation.close else self.broker.lookup_order
                outcome = lookup(self._context, str(order_id))
            elif saved:
                outcome = BrokerOutcome(
                    state=IntentState(str(saved["state"])),
                    request_id=saved["request_id"],
                    broker_order_id=saved.get("broker_order_id"),
                    broker_position_id=saved.get("broker_position_id"),
                    **{
                        name: None if saved.get(name) is None else Decimal(saved[name])
                        for name in ("filled_units", "actual_cost_usd", "realized_pnl_usd", "execution_price")
                    },
                    raw_fingerprint=saved.get("raw_fingerprint"),
                )
            else:
                outcome = None
            if outcome is None:
                unresolved += 1
                continue
            with _runtime_lock:
                self.store.open(self.prefix)
                for entity_type, broker_id in (
                    ("order", outcome.broker_order_id),
                    ("position", outcome.broker_position_id),
                ):
                    if broker_id is not None:
                        self.store.claim_ownership(
                            environment=self._context.environment.value,
                            trading_account_id=self._context.trading_account_id,
                            trading_portfolio_id=self._context.trading_portfolio_id,
                            entity_type=entity_type,
                            broker_id=broker_id,
                            storage_key=self.storage_key,
                        )
                control.state = getattr(ExecutionState, outcome.state.name)
                control.outcome = {**asdict(outcome), "state": outcome.state.value}
                self.store.commit(self.store.control_prefix)
                reservation = self.store.one(Reservation, db0.as_tag(intent), prefix=self.prefix)
                if reservation is not None:
                    self._project_outcome(intent, reservation, outcome, native_copy(intent.params), state, binding)
                    recovered += 1
                if outcome.state in {IntentState.UNKNOWN, IntentState.ACKNOWLEDGED}:
                    unresolved += 1
        snapshot = self.broker.reconcile(self._context)
        with self.store.transaction(self.prefix):
            self.store.append_audit(
                self.prefix,
                state,
                kind="STRATEGY_RECONCILED",
                actor="coordinator",
                source="reconcile",
                facts={"snapshot_fingerprint": hashlib.sha256(_json(snapshot).encode()).hexdigest()},
            )
        return {
            "recovered_intents": recovered,
            "unresolved_intents": unresolved,
            "mirror": "not_applicable" if self.trading_mode == "standalone" else "requires_owner_reconciliation",
        }

    def audit_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        self._validate_binding()
        assert self.store is not None and self.prefix is not None
        if limit < 1 or limit > 1000:
            raise TraderError("INVALID_LIMIT", "limit must be in [1, 1000]")
        events = sorted(self.store.all(AuditEvent, "AUDIT", prefix=self.prefix), key=lambda item: item.sequence)
        return [
            {
                "event_id": str(db0.uuid(event)),
                "sequence": event.sequence,
                "occurred_at": event.occurred_at.isoformat(),
                "kind": event.kind,
                "actor": event.actor,
                "intent_id": str(db0.uuid(event.intent)) if event.intent is not None else None,
                "source": event.source,
                "facts": api_value(event.facts),
                "previous_hash": event.previous_hash,
                "event_hash": event.event_hash,
            }
            for event in events[-limit:]
        ]

    def verify_audit(self) -> dict[str, Any]:
        state, _binding = self._validate_binding()
        assert self.store is not None and self.prefix is not None
        return self.store.verify_audit(self.prefix, state)

    def portfolio_history(self, *, limit: int = 100) -> dict[str, Any]:
        self._validate_binding(active=True)
        assert self.store is not None and self.prefix is not None
        entries = sorted(self.store.all(LedgerEntry, "LEDGER", prefix=self.prefix), key=lambda item: item.occurred_at)
        result: dict[str, list[dict[str, Any]]] = {"strategy": [], "owner_mirror": []}
        for item in entries[-limit:]:
            result[str(item.domain)].append(
                {
                    "entry_id": str(db0.uuid(item)),
                    "kind": str(item.kind),
                    "amount_usd": str(item.amount_usd),
                    "intent_id": str(db0.uuid(item.intent)) if item.intent is not None else None,
                    "occurred_at": item.occurred_at.isoformat(),
                }
            )
        return {"currency": "USD", "series": result, "complete": False, "gaps": ["pre-activation history unavailable"]}

    def trends(self, *, limit: int = 100) -> dict[str, Any]:
        history = self.portfolio_history(limit=limit)
        return {
            "currency": "USD",
            "domains": history["series"],
            "complete": history["complete"],
            "gaps": history["gaps"],
        }
