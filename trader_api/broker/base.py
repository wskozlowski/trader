from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..domain import BrokerMutation, BrokerOutcome, ScopeEvidence, VerifiedContext


@dataclass(frozen=True, slots=True)
class InstrumentSizing:
    instrument_id: int
    amount_usd: Decimal
    units: Decimal
    full_notional_usd: Decimal
    minimum_amount_usd: Decimal
    market_price: Decimal
    settlement_type: str


class ScopeIdentityVerifier(Protocol):
    def verify_scopes(self, api_key: str, user_key: str) -> ScopeEvidence | None: ...


class BrokerAdapter(Protocol):
    def capabilities(self) -> dict[str, bool]: ...

    def verify_identity(self, context: VerifiedContext) -> dict[str, str]: ...

    def state_fingerprint(self, context: VerifiedContext) -> str: ...

    def resolve_sizing(
        self,
        *,
        context: VerifiedContext,
        symbol: str | None,
        instrument_id: int | None,
        strategy_notional_usd: Decimal,
        leverage: int,
        order_type: str,
        side: str,
    ) -> InstrumentSizing: ...

    def estimate_costs(self, *, context: VerifiedContext, sizing: InstrumentSizing, side: str) -> Decimal: ...

    def dispatch(self, context: VerifiedContext, mutation: BrokerMutation) -> BrokerOutcome: ...

    def lookup_request(self, context: VerifiedContext, request_id: str) -> BrokerOutcome | None: ...

    def lookup_order(self, context: VerifiedContext, order_id: str) -> BrokerOutcome | None: ...

    def lookup_close_order(self, context: VerifiedContext, order_id: str) -> BrokerOutcome | None: ...

    def reconcile(self, context: VerifiedContext) -> dict[str, object]: ...
