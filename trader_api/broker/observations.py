"""Typed strategy observations; unknown broker fields never become zeroes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from ..domain import VerifiedContext, decimal_value
from ..errors import TraderError
from ..serialization import storage_datetime


@dataclass(frozen=True, slots=True)
class PortfolioObservation:
    observed_at: datetime
    equity: Decimal | None = None
    cash: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    exposure: Decimal | None = None
    reasons: tuple[str, ...] = ()


class PortfolioReader(Protocol):
    """Optional observational capability, separate from trading/reconciliation."""

    def collect_portfolio(self, context: VerifiedContext, observed_at: datetime) -> PortfolioObservation: ...


def normalize_portfolio(
    payload: Mapping[str, object], context: VerifiedContext, observed_at: datetime
) -> PortfolioObservation:
    """Normalize explicit USD strategy metrics from the identity-bound PnL response.

    Supports flat identity-bearing responses and the child-token clientPortfolio
    envelope. Missing components remain unknown. Neither credit nor policy limits
    imply equity. Unknown envelopes, invalid metrics and mismatched identities fail.
    """
    account = payload.get("accountId", payload.get("accountID"))
    portfolio = payload.get("portfolioId", payload.get("portfolioID"))
    if account is not None or portfolio is not None:
        if str(account) != context.trading_account_id or str(portfolio) != context.trading_portfolio_id:
            raise TraderError("ACCOUNT_MISMATCH", "portfolio observation has a different broker identity")
    elif not isinstance(payload.get("clientPortfolio"), Mapping):
        raise TraderError("VALUATION_UNAVAILABLE", "unsupported portfolio response shape")
    body = payload.get("clientPortfolio", payload)
    if not isinstance(body, Mapping):
        raise TraderError("VALUATION_UNAVAILABLE", "unsupported portfolio response shape")
    currency = body.get("currency", "USD")
    if not isinstance(currency, str) or currency.upper() != "USD":
        raise TraderError("VALUATION_UNAVAILABLE", "portfolio currency is unsupported")
    aliases = {
        "equity": ("equity",),
        "cash": ("cash", "availableCash", "credit"),
        "unrealized_pnl": ("unrealizedPnl", "unrealizedPnL"),
        "exposure": ("exposure",),
    }
    metrics: dict[str, Decimal | None] = {}
    reasons: list[str] = []
    for name, keys in aliases.items():
        raw = next((body[key] for key in keys if body.get(key) is not None), None)
        if isinstance(raw, bool):
            raise TraderError("VALUATION_UNAVAILABLE", "portfolio metric is invalid")
        metrics[name] = None if raw is None else decimal_value(raw, code="VALUATION_UNAVAILABLE")
        if raw is None:
            reasons.append(f"missing_{name}")
    if all(value is None for value in metrics.values()):
        raise TraderError("VALUATION_UNAVAILABLE", "portfolio response has no supported valuation metrics")
    return PortfolioObservation(storage_datetime(observed_at), **metrics, reasons=tuple(reasons))
