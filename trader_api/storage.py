from __future__ import annotations

import gc
import hashlib
import threading
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, TypeVar, cast

import dbzero as db0  # type: ignore[import-untyped]

from .domain import Environment as RuntimeEnvironment
from .domain import utc_now
from .errors import TraderError
from .serialization import canonical_json, native_copy, storage_datetime

T = TypeVar("T")

# dbzero supplies dynamic native attributes; the Python base allocates no slots.
_NATIVE_SLOTS: tuple[str, ...] = ()


@db0.enum(values=["DEMO", "REAL"])
class Environment:
    DEMO: ClassVar[Environment]
    REAL: ClassVar[Environment]


@db0.enum(values=["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "PLN"])
class Currency:
    USD: ClassVar[Currency]
    EUR: ClassVar[Currency]
    GBP: ClassVar[Currency]
    JPY: ClassVar[Currency]
    CHF: ClassVar[Currency]
    CAD: ClassVar[Currency]
    AUD: ClassVar[Currency]
    PLN: ClassVar[Currency]


@db0.enum(values=["open", "close", "modify", "cancel"])
class Operation:
    open: ClassVar[Operation]
    close: ClassVar[Operation]
    modify: ClassVar[Operation]
    cancel: ClassVar[Operation]


@db0.enum(values=["UNBOUND", "PROVISIONING", "VERIFYING", "READY", "ACTIVE", "SUSPENDED", "RETIRED"])
class Lifecycle:
    UNBOUND: ClassVar[Lifecycle]
    PROVISIONING: ClassVar[Lifecycle]
    VERIFYING: ClassVar[Lifecycle]
    READY: ClassVar[Lifecycle]
    ACTIVE: ClassVar[Lifecycle]
    SUSPENDED: ClassVar[Lifecycle]
    RETIRED: ClassVar[Lifecycle]


@db0.enum(
    values=["COMMITTED", "ADMITTED", "ACKNOWLEDGED", "FILLED", "REJECTED", "UNKNOWN", "CANCELED", "RESERVED", "PENDING"]
)
class ExecutionState:
    COMMITTED: ClassVar[ExecutionState]
    ADMITTED: ClassVar[ExecutionState]
    ACKNOWLEDGED: ClassVar[ExecutionState]
    FILLED: ClassVar[ExecutionState]
    REJECTED: ClassVar[ExecutionState]
    UNKNOWN: ClassVar[ExecutionState]
    CANCELED: ClassVar[ExecutionState]
    RESERVED: ClassVar[ExecutionState]
    PENDING: ClassVar[ExecutionState]


@db0.enum(values=["HELD", "RELEASED"])
class ReservationState:
    HELD: ClassVar[ReservationState]
    RELEASED: ClassVar[ReservationState]


@db0.enum(values=["OPEN", "CLOSED"])
class PositionState:
    OPEN: ClassVar[PositionState]
    CLOSED: ClassVar[PositionState]


@db0.enum(values=["COMMITTED", "UNKNOWN", "REJECTED", "REPAIR_REQUIRED", "SECRET_PERSISTED", "READY"])
class ProvisioningState:
    COMMITTED: ClassVar[ProvisioningState]
    UNKNOWN: ClassVar[ProvisioningState]
    REJECTED: ClassVar[ProvisioningState]
    REPAIR_REQUIRED: ClassVar[ProvisioningState]
    SECRET_PERSISTED: ClassVar[ProvisioningState]
    READY: ClassVar[ProvisioningState]


@db0.enum(values=["long", "short"])
class Side:
    long: ClassVar[Side]
    short: ClassVar[Side]


@db0.enum(values=["strategy", "owner_mirror"])
class LedgerDomain:
    strategy: ClassVar[LedgerDomain]
    owner_mirror: ClassVar[LedgerDomain]


@db0.enum(values=["ACTUAL_FILL", "RECONCILED_ACTUAL"])
class LedgerKind:
    ACTUAL_FILL: ClassVar[LedgerKind]
    RECONCILED_ACTUAL: ClassVar[LedgerKind]


@db0.enum(values=["order", "position"])
class EntityType:
    order: ClassVar[EntityType]
    position: ClassVar[EntityType]


@db0.memo(immutable=True)
@db0.tag_fields("name")
@dataclass(eq=False)
class Trader:
    __slots__ = _NATIVE_SLOTS

    name: str


@db0.memo(singleton=True)
@db0.tag_fields("trader")
@dataclass(eq=False)
class TraderState:
    __slots__ = _NATIVE_SLOTS

    trader: Trader
    environment: Environment
    initialized: bool = field(default=False, kw_only=True)
    currency: Currency = field(default=Currency.USD, kw_only=True)
    strategy_initial_cap: Decimal = field(default=Decimal("0.00"), kw_only=True)
    owner_initial_cap: Decimal = field(default=Decimal("0.00"), kw_only=True)
    strategy_realized: Decimal = field(default=Decimal("0.00"), kw_only=True)
    owner_realized: Decimal = field(default=Decimal("0.00"), kw_only=True)
    strategy_committed: Decimal = field(default=Decimal("0.00"), kw_only=True)
    owner_committed: Decimal = field(default=Decimal("0.00"), kw_only=True)
    policy_version: int = field(default=1, kw_only=True)
    audit_sequence: int = field(default=0, kw_only=True)
    audit_head: str = field(default="0" * 64, kw_only=True)


@db0.memo(singleton=True)
@db0.tag_fields("trader")
@dataclass(eq=False)
class PortfolioBinding:
    __slots__ = _NATIVE_SLOTS

    environment: Environment | None = field(default=None, kw_only=True)
    trader: Trader | None = field(default=None, kw_only=True)
    # Supplied owner identity; eToro adapter uses credential:<fingerprint>, not an account ID.
    owner_account_id: str | None = field(default=None, kw_only=True)
    # External eToro agentPortfolioId UUID, not a dbzero UUID.
    agent_portfolio_id: str | None = field(default=None, kw_only=True)
    # External eToro numeric agentPortfolioGcid represented as text.
    agent_portfolio_gcid: str | None = field(default=None, kw_only=True)
    # Credential-bound broker account identity; owner issuance uses the agent GCID.
    agent_trading_account_id: str | None = field(default=None, kw_only=True)
    # Credential-bound broker portfolio identity; owner issuance uses agentPortfolioId.
    agent_trading_portfolio_id: str | None = field(default=None, kw_only=True)
    # External eToro numeric mirrorId for the copy relationship, represented as text.
    mirror_id: str | None = field(default=None, kw_only=True)
    investment_usd: Decimal = field(default=Decimal("0.00"), kw_only=True)
    virtual_balance_usd: Decimal = field(default=Decimal("0.00"), kw_only=True)
    lifecycle: Lifecycle = field(default=Lifecycle.UNBOUND, kw_only=True)
    binding_version: int = field(default=0, kw_only=True)
    copy_healthy: bool = field(default=False, kw_only=True)
    credential_fingerprint: str | None = field(default=None, kw_only=True)
    scope_names: list[str] = field(default_factory=list, kw_only=True)
    verified_at: datetime | None = field(default=None, kw_only=True)


@db0.memo(singleton=True)
@dataclass(eq=False)
class DirectAccount:
    __slots__ = _NATIVE_SLOTS

    # Separate from owner-issued agent portfolio identity and copy relationship.
    account_id: str = field(default="", kw_only=True)
    portfolio_id: str = field(default="", kw_only=True)
    credential_fingerprint: str = field(default="", kw_only=True)
    identity_source: str = field(default="", kw_only=True)
    environment: Environment | None = field(default=None, kw_only=True)
    lifecycle: Lifecycle = field(default=Lifecycle.UNBOUND, kw_only=True)
    verified_at: datetime | None = field(default=None, kw_only=True)


@db0.memo
@db0.tag_fields("operation")
@dataclass(eq=False)
class Preview:
    __slots__ = _NATIVE_SLOTS

    operation: Operation
    # External eToro instrument_id (int), position_id/order_id (numeric text), and market symbol.
    params: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    state_fingerprint: str
    binding_version: int
    policy_version: int


@db0.memo
@db0.indexed_fields("sequence", "created_at")
@db0.tag_fields("preview", "request_id", "operation", "state")
@dataclass(eq=False)
class Intent:
    __slots__ = _NATIVE_SLOTS

    preview: Preview
    # Caller-supplied key for local submission deduplication, not a broker or memo ID.
    idempotency_key: str
    operation: Operation
    # Snapshot of Preview.params, retaining the same external eToro identifiers.
    params: dict[str, Any]
    # Locally generated UUID sent as eToro x-request-id and queried via referenceId; not a memo ID.
    request_id: str
    command_digest: str
    binding_version: int
    policy_version: int
    state: ExecutionState = field(default=ExecutionState.COMMITTED, kw_only=True)
    created_at: datetime = field(default_factory=utc_now, kw_only=True)
    # External eToro numeric orderId/orderID as text; absent until reported by the broker.
    broker_order_id: str | None = field(default=None, kw_only=True)
    # External eToro numeric positionId/positionID as text; absent until reported by the broker.
    broker_position_id: str | None = field(default=None, kw_only=True)
    error_code: str | None = field(default=None, kw_only=True)
    sequence: int = field(default_factory=lambda: next_sequence(), kw_only=True)
    projected_state: ExecutionState | None = field(default=None, kw_only=True)


@db0.memo
@db0.tag_fields("intent")
@dataclass(eq=False)
class Reservation:
    __slots__ = _NATIVE_SLOTS

    intent: Intent
    strategy_amount_usd: Decimal
    owner_amount_usd: Decimal
    binding_version: int
    policy_version: int
    state: ReservationState = field(default=ReservationState.HELD, kw_only=True)


@db0.memo
@db0.indexed_fields("sequence", "created_at")
@db0.tag_fields("intent", "position_id", "state", "symbol", "symbol_filter")
@dataclass(eq=False)
class Position:
    __slots__ = _NATIVE_SLOTS

    # External eToro numeric positionId/positionID as text for broker operations; not a memo ID.
    position_id: str
    intent: Intent
    # Instrument market symbol, not a dbzero identifier.
    symbol: str
    side: Side
    # External eToro numeric instrumentId/instrumentID for quotes and orders; not a memo ID.
    instrument_id: int
    leverage: int
    strategy_notional_usd: Decimal
    units: Decimal
    stop_loss_rate: Decimal | None = field(default=None, kw_only=True)
    take_profit_rate: Decimal | None = field(default=None, kw_only=True)
    state: PositionState = field(default=PositionState.OPEN, kw_only=True)
    sequence: int = field(default_factory=lambda: next_sequence(), kw_only=True)
    created_at: datetime = field(default_factory=utc_now, kw_only=True)
    symbol_filter: str = field(init=False)

    def __post_init__(self) -> None:
        self.symbol_filter = f"symbol:{self.symbol}"


@db0.memo
@db0.indexed_fields("sequence", "created_at")
@db0.tag_fields("intent", "order_id", "state", "symbol", "symbol_filter")
@dataclass(eq=False)
class Order:
    __slots__ = _NATIVE_SLOTS

    # External eToro numeric orderId/orderID as text for lookup/cancellation; not a memo ID.
    order_id: str
    intent: Intent
    # Instrument market symbol.
    symbol: str
    state: ExecutionState
    sequence: int = field(default_factory=lambda: next_sequence(), kw_only=True)
    created_at: datetime = field(default_factory=utc_now, kw_only=True)
    symbol_filter: str = field(init=False)

    def __post_init__(self) -> None:
        self.symbol_filter = f"symbol:{self.symbol}"


@db0.memo(singleton=True)
@dataclass(eq=False)
class LocalAccountingVersion:
    __slots__ = _NATIVE_SLOTS

    initialized: bool = False


@db0.memo
@db0.tag_fields("intent")
@dataclass(eq=False)
class ExecutionFill:
    __slots__ = _NATIVE_SLOTS

    """Confirmed execution facts, separate from legacy position schemas and estimates."""

    intent: Intent
    units: Decimal | None = None
    price: Decimal | None = None
    costs: Decimal | None = None
    gross_pnl: Decimal | None = None
    budget_net: Decimal = Decimal("0")
    cost_reservation_released: bool = False


@db0.memo
@db0.tag_fields("position")
@dataclass(eq=False)
class PositionPrice:
    __slots__ = _NATIVE_SLOTS

    position: Position
    bid: Decimal
    ask: Decimal
    refreshed_at: datetime


@dataclass(eq=False)
class BaseEvent:
    __slots__ = _NATIVE_SLOTS

    sequence: int
    occurred_at: datetime
    kind: str
    facts: dict[str, Any]
    previous_hash: str
    event_hash: str


@db0.memo(immutable=True)
@db0.indexed_fields("sequence", "occurred_at")
@db0.tag_fields("intent", "kind", "actor", "source", "kind_filter", "actor_filter", "source_filter")
@dataclass(eq=False)
class AuditEvent(BaseEvent):
    __slots__ = _NATIVE_SLOTS

    actor: str
    intent: Intent | None
    source: str
    kind_filter: str = field(init=False)
    actor_filter: str = field(init=False)
    source_filter: str = field(init=False)

    def __post_init__(self) -> None:
        self.kind_filter = f"kind:{self.kind}"
        self.actor_filter = f"actor:{self.actor}"
        self.source_filter = f"source:{self.source}"


@db0.memo(immutable=True)
@db0.tag_fields("intent", "domain")
@dataclass(eq=False)
class LedgerEntry:
    __slots__ = _NATIVE_SLOTS

    domain: LedgerDomain
    kind: LedgerKind
    amount_usd: Decimal
    intent: Intent | None
    occurred_at: datetime


@db0.memo
@db0.tag_fields("trader")
@dataclass(eq=False)
class TraderRegistration:
    __slots__ = _NATIVE_SLOTS

    trader_hash: str
    trader: Trader
    storage_key: str
    service_credential_hash: str
    authorized: bool = field(default=True, kw_only=True)


@db0.memo
@dataclass(eq=False)
class ControlReservation:
    __slots__ = _NATIVE_SLOTS

    command_digest: str
    storage_key: str
    # Intent's locally generated broker correlation UUID sent as x-request-id; not a memo ID.
    request_id: str
    binding_version: int
    state: ExecutionState = field(default=ExecutionState.RESERVED, kw_only=True)
    # Retains the external request/order/position identifiers documented on BrokerOutcome fields.
    outcome: dict[str, Any] | None = field(default=None, kw_only=True)


@db0.memo
@dataclass(eq=False)
class ProvisioningIntent:
    __slots__ = _NATIVE_SLOTS

    request_key_digest: str
    # Locally generated correlation UUID sent to eToro as x-request-id; not a memo ID.
    request_id: str
    trader_hash: str
    portfolio_name: str
    investment_usd: Decimal
    scopes: list[str]
    state: ProvisioningState = field(default=ProvisioningState.COMMITTED, kw_only=True)
    # External eToro agentPortfolioId UUID, not this memo's identity.
    agent_portfolio_id: str | None = field(default=None, kw_only=True)
    # Local vault locator for the child credential, not a broker ID or the secret itself.
    credential_reference: str | None = field(default=None, kw_only=True)
    error_code: str | None = field(default=None, kw_only=True)


@db0.memo(immutable=True)
@dataclass(eq=False)
class OwnershipClaim:
    __slots__ = _NATIVE_SLOTS

    scoped_key_digest: str
    storage_key: str
    entity_type: EntityType
    # External eToro numeric order/position ID as text, selected by entity_type; not a memo ID.
    broker_id: str


@db0.memo
@dataclass(eq=False)
class TokenVerification:
    __slots__ = _NATIVE_SLOTS

    credential_fingerprint: str
    scopes: list[str]
    # Broker credential subject from scope evidence; owner issuance uses the agent GCID.
    subject_id: str
    # Authorized broker account identity; owner issuance uses the agent GCID, not a memo ID.
    trading_account_id: str
    # Authorized broker portfolio identity; owner issuance uses the agentPortfolioId UUID.
    trading_portfolio_id: str
    issued_at: datetime
    expires_at: datetime | None
    source: str
    revoked: bool = field(default=False, kw_only=True)


@db0.memo(singleton=True)
@dataclass(eq=False)
class ControlState:
    __slots__ = _NATIVE_SLOTS

    audit_sequence: int = field(default=0, kw_only=True)
    audit_head: str = field(default="0" * 64, kw_only=True)


@db0.memo(immutable=True)
@db0.indexed_fields("sequence", "occurred_at")
@dataclass(eq=False)
class ControlEvent(BaseEvent):
    __slots__ = _NATIVE_SLOTS

    pass


@db0.memo(singleton=True)
@dataclass(eq=False)
class UiSequence:
    __slots__ = _NATIVE_SLOTS

    value: int = 0


def next_sequence() -> int:
    counter = UiSequence()
    counter.value += 1
    return counter.value


@db0.memo(singleton=True)
@dataclass(eq=False)
class RefreshState:
    __slots__ = _NATIVE_SLOTS

    last_attempt: datetime | None = None
    last_success: datetime | None = None
    next_attempt: datetime | None = None
    in_progress: bool = False
    error: str | None = None
    failures: int = 0


@db0.memo(singleton=True)
@dataclass(eq=False)
class CurrentSummary:
    __slots__ = _NATIVE_SLOTS

    started_at: datetime
    generation: int = 0
    accounting_checkpoint: int = 0
    last_observed_at: datetime | None = None
    equity: Decimal | None = None
    cash: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    exposure: Decimal | None = None
    realized_pnl: Decimal | None = None
    costs: Decimal | None = None
    operation_counts: dict[str, int] = field(default_factory=dict)
    unconfirmed_costs: int = 0
    unconfirmed_realized_pnl: int = 0
    reasons: tuple[str, ...] = ("valuation_not_collected",)


@db0.memo(immutable=True)
@db0.indexed_fields("sequence", "occurred_at")
@dataclass(eq=False)
class ValuationSnapshot:
    __slots__ = _NATIVE_SLOTS

    sequence: int
    occurred_at: datetime
    equity: Decimal | None
    cash: Decimal | None
    unrealized_pnl: Decimal | None
    exposure: Decimal | None
    reasons: tuple[str, ...]
    generation: int


@db0.memo(immutable=True)
@db0.indexed_fields("sequence", "occurred_at")
@db0.tag_fields("intent", "kind")
@dataclass(eq=False)
class AccountingObservation:
    __slots__ = _NATIVE_SLOTS

    sequence: int
    occurred_at: datetime
    # Memo entity handle for the local operation, never a generated link.
    intent: Intent
    kind: str
    amount: Decimal | None
    delta: Decimal | None
    confirmed: bool
    generation: int


@db0.memo
@db0.tag_fields("intent")
@dataclass(eq=False)
class OperationContribution:
    __slots__ = _NATIVE_SLOTS

    # Local operation handle used as the processing checkpoint key.
    intent: Intent
    state: str | None = None
    costs: Decimal | None = None
    realized_pnl: Decimal | None = None
    estimated_costs: Decimal | None = None
    costs_missing: bool = False
    realized_pnl_missing: bool = False


@dataclass(eq=False)
class EquityAggregate:
    __slots__ = _NATIVE_SLOTS

    first_equity: Decimal | None = field(default=None, kw_only=True)
    last_equity: Decimal | None = field(default=None, kw_only=True)
    min_equity: Decimal | None = field(default=None, kw_only=True)
    max_equity: Decimal | None = field(default=None, kw_only=True)
    max_drawdown: Decimal | None = field(default=None, kw_only=True)
    cash: Decimal | None = field(default=None, kw_only=True)
    unrealized_pnl: Decimal | None = field(default=None, kw_only=True)
    exposure: Decimal | None = field(default=None, kw_only=True)
    sample_count: int = field(default=0, kw_only=True)
    first_observed_at: datetime | None = field(default=None, kw_only=True)
    last_observed_at: datetime | None = field(default=None, kw_only=True)
    missing_intervals: int = field(default=0, kw_only=True)
    reasons: tuple[str, ...] = field(default=(), kw_only=True)
    generation: int = field(default=0, kw_only=True)


@db0.memo
@db0.indexed_fields("start")
@db0.tag_fields("key", "period")
@dataclass(eq=False)
class PeriodSummary(EquityAggregate):
    __slots__ = _NATIVE_SLOTS

    key: str
    period: str
    start: datetime
    realized_pnl: Decimal | None = None
    costs: Decimal | None = None
    operation_counts: dict[str, int] = field(default_factory=dict)
    unconfirmed_costs: int = 0
    unconfirmed_realized_pnl: int = 0


@db0.memo
@db0.indexed_fields("start")
@db0.tag_fields("key", "resolution")
@dataclass(eq=False)
class ChartBucket(EquityAggregate):
    __slots__ = _NATIVE_SLOTS

    key: str
    resolution: str
    start: datetime


# Native memo wrappers store their fields in dbzero. Empty Python slots above
# prevent inheriting managed dict/weakref layouts incompatible with that native
# storage on CPython 3.14; persisted field names and schemas are unchanged.
class _DatabaseLock:
    """Serialize native access and defer cyclic GC while dbzero 0.6.6 releases the GIL.

    This pinned extension can race GC traversal with native commit (see
    test_dbzero_repro.py). Refcounting continues normally; automatic cyclic GC
    resumes immediately after the outer critical section, including on errors.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._restore_gc = False

    def __enter__(self) -> None:
        self._lock.acquire()
        if self._depth == 0:
            self._restore_gc = gc.isenabled()
            gc.disable()
        self._depth += 1

    def __exit__(self, *exc: object) -> None:
        self._depth -= 1
        if self._depth == 0 and self._restore_gc:
            gc.enable()
        self._lock.release()


_runtime_lock = _DatabaseLock()
_runtime_root: Path | None = None
_runtime_epoch = object()


def close_dbzero() -> None:
    global _runtime_root, _runtime_epoch
    with _runtime_lock:
        if _runtime_root is not None:
            db0.close()
            _runtime_root = None
            _runtime_epoch = object()


def current_dbzero_root() -> Path | None:
    return _runtime_root


def current_dbzero_epoch() -> object:
    return _runtime_epoch


class DbzeroStore:
    """Prefix-scoped persistence. Prefix names are never accepted from callers."""

    def __init__(self, root: Path, environment: RuntimeEnvironment) -> None:
        resolved = root.resolve()
        allowed = Path(
            "/dbzero-data/trader-dev" if environment is RuntimeEnvironment.DEMO else "/dbzero-data/trader"
        ).resolve()
        if resolved != allowed and (environment is RuntimeEnvironment.REAL or allowed not in resolved.parents):
            raise TraderError("STORAGE_INVALID", "dbzero root is outside the environment allowlist")
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = resolved
        self.environment = environment
        self.control_prefix = f"/trader/{environment.value}/control"
        global _runtime_root
        with _runtime_lock:
            if _runtime_root is None:
                db0.init(str(resolved), autocommit=False, async_autocommit=False, file_prealloc=False)
                _runtime_root = resolved
            elif _runtime_root != resolved:
                raise TraderError("STORAGE_INVALID", "a process cannot open two dbzero physical roots")
            self.open(self.control_prefix)

    @staticmethod
    def trader_hash(trader_id: str) -> str:
        return hashlib.sha256(trader_id.encode()).hexdigest()

    def open(self, prefix: str, mode: str = "rw") -> None:
        """Select the active prefix, including when it is already open."""
        db0.open(prefix, mode, autocommit=False, restricted=True)

    def register(self, trader_id: str, service_credential: str) -> TraderRegistration:
        trader_hash = self.trader_hash(trader_id)
        existing = self.one(TraderRegistration, f"trader:{trader_hash}", prefix=self.control_prefix)
        credential_hash = hashlib.sha256(service_credential.encode()).hexdigest()
        if existing is not None:
            self.trader_prefix(existing.storage_key)
            if existing.trader.name != trader_id or existing.service_credential_hash != credential_hash:
                raise TraderError("TRADER_MISMATCH", "trader registration does not match credentials")
            return existing
        storage_key = str(uuid.uuid4())
        prefix = self.trader_prefix(storage_key)
        trader = Trader(trader_id)
        self.tag(trader, "TRADER")
        self.commit(prefix)
        self.open(self.control_prefix)
        item = TraderRegistration(trader_hash, trader, storage_key, credential_hash)
        db0.tags(item).add([f"trader:{trader_hash}", f"storage:{storage_key}"])
        self.append_control_event("TRADER_REGISTERED", {"trader_hash": trader_hash})
        db0.commit(self.control_prefix)
        return item

    def authenticate(self, trader_id: str, service_credential: str | None) -> TraderRegistration:
        trader_hash = self.trader_hash(trader_id)
        registration = self.one(TraderRegistration, f"trader:{trader_hash}", prefix=self.control_prefix)
        if registration is None or not registration.authorized or service_credential is None:
            raise TraderError("AUTHENTICATION_REQUIRED", "valid local service credentials are required")
        presented = hashlib.sha256(service_credential.encode()).hexdigest()
        if not __import__("hmac").compare_digest(presented, str(registration.service_credential_hash)):
            raise TraderError("AUTHENTICATION_REQUIRED", "valid local service credentials are required")
        self.trader_prefix(registration.storage_key)
        if registration.trader.name != trader_id:
            raise TraderError("TRADER_MISMATCH", "authenticated trader identity mismatch")
        return registration

    def one_registration_exists(self, trader_id: str) -> bool:
        return (
            self.one(TraderRegistration, f"trader:{self.trader_hash(trader_id)}", prefix=self.control_prefix)
            is not None
        )

    def trader_prefix(self, storage_key: str) -> str:
        try:
            normalized = str(uuid.UUID(storage_key))
        except ValueError as exc:
            raise TraderError("STORAGE_INVALID", "invalid opaque trader storage key") from exc
        prefix = f"/trader/{self.environment.value}/traders/{normalized}"
        self.open(prefix)
        return prefix

    def one(self, model: type[T], tag: Any = None, *, prefix: str) -> T | None:
        self.open(prefix)
        query = db0.find(model, *([tag] if tag is not None else []), prefix=prefix)
        return cast(T | None, next(iter(query), None))

    def all(self, model: type[T], *tags: Any, prefix: str) -> list[T]:
        self.open(prefix)
        return cast(list[T], list(db0.find(model, *tags, prefix=prefix)))

    def tag(self, item: Any, *tags: str) -> None:
        db0.tags(item).add(list(tags))

    def commit(self, prefix: str) -> None:
        db0.commit(prefix)

    @contextmanager
    def transaction(self, prefix: str) -> Iterator[None]:
        """Serialize a short source/aggregate update and publish it as one commit."""
        with _runtime_lock:
            self.open(prefix)
            with db0.atomic():
                yield
            self.commit(prefix)

    def state(self, prefix: str, trader: Trader) -> TraderState:
        self.open(prefix)
        if db0.get_prefix_of(trader).name != prefix.lstrip("/"):
            raise TraderError("TRADER_MISMATCH", "trader belongs to a different storage prefix")
        environment = Environment.DEMO if self.environment is RuntimeEnvironment.DEMO else Environment.REAL
        state = TraderState(trader, environment)
        if state.trader != trader:
            raise TraderError("TRADER_MISMATCH", "state belongs to a different trader")
        if state.environment != environment:
            raise TraderError("PORTFOLIO_SCOPE_MISMATCH", "state environment does not match storage environment")
        return state

    def binding(self, prefix: str) -> PortfolioBinding:
        self.open(prefix)
        return PortfolioBinding()

    def append_audit(
        self,
        prefix: str,
        state: TraderState,
        *,
        kind: str,
        actor: str,
        intent: Intent | None = None,
        source: str = "trader",
        facts: dict[str, Any] | None = None,
    ) -> AuditEvent:
        self.open(prefix)
        safe_facts = native_copy(facts or {})
        sequence = int(state.audit_sequence) + 1
        occurred_at = storage_datetime(utc_now())
        command_digest = intent.command_digest if intent is not None else ""
        body = canonical_json(
            [sequence, occurred_at, kind, actor, command_digest, source, safe_facts, state.audit_head],
        )
        event_hash = hashlib.sha256(body.encode()).hexdigest()
        event = AuditEvent(
            sequence=sequence,
            occurred_at=occurred_at,
            kind=kind,
            actor=actor,
            intent=intent,
            source=source,
            facts=safe_facts,
            previous_hash=str(state.audit_head),
            event_hash=event_hash,
        )
        self.tag(event, "AUDIT")
        state.audit_sequence = sequence
        state.audit_head = event_hash
        if intent is not None:
            from .ui_api.updates import record_operation

            record_operation(intent, occurred_at)
        return event

    def verify_audit(self, prefix: str, state: TraderState) -> dict[str, Any]:
        events = sorted(self.all(AuditEvent, "AUDIT", prefix=prefix), key=lambda item: item.sequence)
        previous = "0" * 64
        for expected_sequence, event in enumerate(events, 1):
            body = canonical_json(
                [
                    event.sequence,
                    event.occurred_at,
                    event.kind,
                    event.actor,
                    event.intent.command_digest if event.intent is not None else "",
                    event.source,
                    event.facts,
                    event.previous_hash,
                ],
            )
            expected_hash = hashlib.sha256(body.encode()).hexdigest()
            if (
                event.sequence != expected_sequence
                or event.previous_hash != previous
                or event.event_hash != expected_hash
            ):
                return {"valid": False, "checked_events": expected_sequence - 1, "head": previous}
            previous = event.event_hash
        valid = previous == state.audit_head and len(events) == int(state.audit_sequence)
        return {"valid": valid, "checked_events": len(events), "head": previous}

    def assert_prefix_isolation(self) -> None:
        registrations = self.all(TraderRegistration, prefix=self.control_prefix)
        storage_keys = [item.storage_key for item in registrations]
        if len(storage_keys) != len(set(storage_keys)):
            raise TraderError("STORAGE_INVALID", "a trader storage prefix has multiple registrations")
        if self.all(TraderState, prefix=self.control_prefix):
            raise TraderError("STORAGE_INVALID", "trader data exists in the control prefix")

    def reserve_control(
        self,
        *,
        command_digest: str,
        storage_key: str,
        request_id: str,
        binding_version: int,
    ) -> ControlReservation:
        existing = self.one(ControlReservation, f"command:{command_digest}", prefix=self.control_prefix)
        if existing is not None:
            return existing
        reservation = ControlReservation(command_digest, storage_key, request_id, binding_version)
        self.tag(reservation, f"command:{command_digest}", f"storage:{storage_key}", "CONTROL_RESERVATION")
        self.append_control_event(
            "CONTROL_RESERVATION_COMMITTED",
            {"command_digest": command_digest, "request_id": request_id},
        )
        self.commit(self.control_prefix)
        return reservation

    def unresolved_control(self, storage_key: str) -> Iterable[ControlReservation]:
        return self.all(ControlReservation, f"storage:{storage_key}", prefix=self.control_prefix)

    def claim_ownership(
        self,
        *,
        environment: str,
        trading_account_id: str,
        trading_portfolio_id: str,
        entity_type: str,
        broker_id: str,
        storage_key: str,
    ) -> None:
        scoped_key = "\x1f".join([environment, trading_account_id, trading_portfolio_id, entity_type, broker_id])
        digest = hashlib.sha256(scoped_key.encode()).hexdigest()
        existing = self.one(OwnershipClaim, f"ownership:{digest}", prefix=self.control_prefix)
        if existing is not None:
            if existing.storage_key != storage_key:
                raise TraderError("BROKER_CAPACITY_UNAVAILABLE", "broker entity is already claimed")
            return
        claim = OwnershipClaim(digest, storage_key, getattr(EntityType, entity_type), broker_id)
        self.tag(claim, f"ownership:{digest}", f"storage:{storage_key}", "OWNERSHIP")
        self.append_control_event("OWNERSHIP_CLAIMED", {"scoped_key_digest": digest, "entity_type": entity_type})

    def record_scope_evidence(self, credential_fingerprint: str, evidence: Any) -> None:
        tag = f"credential:{credential_fingerprint}"
        existing = self.one(TokenVerification, tag, prefix=self.control_prefix)
        identity = (
            evidence.subject_id,
            evidence.trading_account_id,
            evidence.trading_portfolio_id,
        )
        if existing is not None:
            if (
                existing.subject_id,
                existing.trading_account_id,
                existing.trading_portfolio_id,
            ) != identity:
                raise TraderError("ACCOUNT_MISMATCH", "credential evidence conflicts with its immutable identity")
            existing.scopes = sorted(evidence.scopes)
            existing.expires_at = evidence.expires_at
            existing.revoked = bool(evidence.revoked)
        else:
            existing = TokenVerification(
                credential_fingerprint,
                sorted(evidence.scopes),
                evidence.subject_id,
                evidence.trading_account_id,
                evidence.trading_portfolio_id,
                evidence.issued_at,
                evidence.expires_at,
                evidence.source,
            )
            self.tag(existing, tag, "TOKEN_VERIFICATION")
        self.append_control_event(
            "TOKEN_EVIDENCE_RECORDED",
            {"credential_fingerprint": credential_fingerprint, "revoked": bool(evidence.revoked)},
        )
        self.commit(self.control_prefix)

    def scope_evidence(self, credential_fingerprint: str) -> TokenVerification | None:
        return self.one(
            TokenVerification,
            f"credential:{credential_fingerprint}",
            prefix=self.control_prefix,
        )

    def append_control_event(self, kind: str, facts: dict[str, Any]) -> ControlEvent:
        self.open(self.control_prefix)
        state = ControlState()
        sequence = int(state.audit_sequence) + 1
        occurred_at = storage_datetime(utc_now())
        facts = native_copy(facts)
        body = canonical_json([sequence, occurred_at, kind, facts, state.audit_head])
        event_hash = hashlib.sha256(body.encode()).hexdigest()
        event = ControlEvent(
            sequence=sequence,
            occurred_at=occurred_at,
            kind=kind,
            facts=facts,
            previous_hash=str(state.audit_head),
            event_hash=event_hash,
        )
        self.tag(event, "CONTROL_AUDIT", f"kind:{kind}")
        state.audit_sequence = sequence
        state.audit_head = event_hash
        return event

    def verify_control_audit(self) -> dict[str, Any]:
        self.open(self.control_prefix)
        state = ControlState()
        events = sorted(
            self.all(ControlEvent, "CONTROL_AUDIT", prefix=self.control_prefix),
            key=lambda item: item.sequence,
        )
        previous = "0" * 64
        for expected_sequence, event in enumerate(events, 1):
            body = canonical_json(
                [
                    event.sequence,
                    event.occurred_at,
                    event.kind,
                    event.facts,
                    event.previous_hash,
                ],
            )
            expected_hash = hashlib.sha256(body.encode()).hexdigest()
            if (
                event.sequence != expected_sequence
                or event.previous_hash != previous
                or event.event_hash != expected_hash
            ):
                return {"valid": False, "checked_events": expected_sequence - 1}
            previous = event.event_hash
        return {
            "valid": previous == state.audit_head and len(events) == int(state.audit_sequence),
            "checked_events": len(events),
            "head": previous,
        }

    def backup_prefix(self, prefix: str, destination: Path) -> Path:
        resolved = destination.resolve()
        if self.root not in resolved.parents:
            raise TraderError("STORAGE_INVALID", "backup must remain inside the environment root")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        db0.commit(prefix)
        db0.copy_prefix(str(resolved), prefix=prefix)
        return resolved
