from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from .errors import TraderError

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def decimal_value(value: object, *, code: str = "INVALID_AMOUNT") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TraderError(code, "value must be a decimal") from exc
    if not result.is_finite():
        raise TraderError(code, "value must be finite")
    return result


def money(value: object, *, allow_negative: bool = False) -> Decimal:
    result = decimal_value(value)
    if not allow_negative and result < 0:
        raise TraderError("INVALID_AMOUNT", "amount must be non-negative")
    return result.quantize(CENT)


def reserve_money(value: object) -> Decimal:
    result = decimal_value(value)
    if result < 0:
        raise TraderError("INVALID_AMOUNT", "amount must be non-negative")
    return result.quantize(CENT, rounding=ROUND_UP)


def utc_now() -> datetime:
    return datetime.now(UTC)


class Environment(StrEnum):
    DEMO = "demo"
    REAL = "real"


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"


class OrderType(StrEnum):
    MARKET = "market"
    MARKET_IF_TOUCHED = "market_if_touched"
    LIMIT_IOC = "limit_ioc"


class Lifecycle(StrEnum):
    UNBOUND = "UNBOUND"
    PROVISIONING = "PROVISIONING"
    VERIFYING = "VERIFYING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


class IntentState(StrEnum):
    COMMITTED = "COMMITTED"
    ADMITTED = "ADMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    CANCELED = "CANCELED"


@dataclass(frozen=True, slots=True)
class ScopeEvidence:
    scopes: frozenset[str]
    subject_id: str
    trading_account_id: str
    trading_portfolio_id: str
    issued_at: datetime
    expires_at: datetime | None = None
    revoked: bool = False
    source: str = "owner_metadata"


@dataclass(frozen=True, slots=True)
class VerifiedContext:
    environment: Environment
    subject_id: str
    trading_account_id: str
    trading_portfolio_id: str
    credential_fingerprint: str
    scopes: frozenset[str]
    verified_at: datetime
    can_read: bool
    can_write: bool


@dataclass(frozen=True, slots=True)
class PortfolioBindingValue:
    environment: Environment
    trader_id: str
    owner_account_id: str
    agent_portfolio_id: str
    agent_portfolio_gcid: str
    agent_trading_account_id: str
    agent_trading_portfolio_id: str
    mirror_id: str
    investment_usd: Decimal
    virtual_balance_usd: Decimal
    lifecycle: Lifecycle
    binding_version: int
    copy_healthy: bool


@dataclass(frozen=True, slots=True)
class BrokerMutation:
    request_id: str
    operation: str
    payload: dict[str, Any]
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrokerOutcome:
    state: IntentState
    request_id: str
    broker_order_id: str | None = None
    broker_position_id: str | None = None
    filled_units: Decimal | None = None
    actual_cost_usd: Decimal | None = None
    realized_pnl_usd: Decimal | None = None
    raw_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class Budget:
    initial_cap: Decimal
    settled_realized_pnl: Decimal
    committed: Decimal

    @property
    def capital_ceiling(self) -> Decimal:
        return money(self.initial_cap + self.settled_realized_pnl, allow_negative=True)

    @property
    def available_to_open(self) -> Decimal:
        return money(max(ZERO, self.capital_ceiling - self.committed))

    def as_dict(self) -> dict[str, str]:
        return {
            "initial_policy_cap_usd": str(self.initial_cap),
            "settled_net_realized_pnl_usd": str(self.settled_realized_pnl),
            "committed_usd": str(self.committed),
            "capital_ceiling_usd": str(self.capital_ceiling),
            "available_to_open_usd": str(self.available_to_open),
        }
