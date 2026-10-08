from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ..domain import BrokerMutation, BrokerOutcome, ScopeEvidence, VerifiedContext


@dataclass(frozen=True, slots=True)
class InstrumentSizing:
    # External eToro numeric instrumentId from eligibility, for quotes/costs/orders; not a memo ID.
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
        # External eToro instrument catalogue ID, not a memo ID.
        instrument_id: int | None,
        strategy_notional_usd: Decimal,
        leverage: int,
        order_type: str,
        side: str,
    ) -> InstrumentSizing: ...

    def estimate_costs(self, *, context: VerifiedContext, sizing: InstrumentSizing, side: str) -> Decimal: ...

    def dispatch(self, context: VerifiedContext, mutation: BrokerMutation) -> BrokerOutcome: ...

    def lookup_request(
        self,
        context: VerifiedContext,
        # Locally generated x-request-id correlation UUID queried via eToro referenceId.
        request_id: str,
    ) -> BrokerOutcome | None: ...

    def lookup_order(
        self,
        context: VerifiedContext,
        # External eToro numeric order ID represented as text.
        order_id: str,
    ) -> BrokerOutcome | None: ...

    def lookup_close_order(
        self,
        context: VerifiedContext,
        # External eToro numeric close-order ID represented as text.
        order_id: str,
    ) -> BrokerOutcome | None: ...

    def reconcile(self, context: VerifiedContext) -> dict[str, object]: ...
