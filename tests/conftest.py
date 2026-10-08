from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import trader_api.config as config_module
from trader_api.auth import DEMO_READ, DEMO_WRITE, fingerprint
from trader_api.broker.base import InstrumentSizing
from trader_api.domain import (
    BrokerMutation,
    BrokerOutcome,
    Environment,
    IntentState,
    ScopeEvidence,
    VerifiedContext,
)
from trader_api.provisioning import OwnerAdminService
from trader_api.storage import close_dbzero


class StaticVerifier:
    def __init__(self, evidence: ScopeEvidence) -> None:
        self.evidence = evidence

    def verify(self, *, api_key: str, user_key: str) -> ScopeEvidence:
        return self.evidence


class FakeBroker:
    def __init__(self) -> None:
        self.fingerprint = "state-1"
        self.mutations: list[BrokerMutation] = []
        self.outcome_state = IntentState.FILLED

    def capabilities(self) -> dict[str, bool]:
        return {
            "market": True,
            "market_if_touched": True,
            "limit_ioc": True,
            "cancel": True,
            "partial_close": True,
            "modify_protection": True,
            "request_lookup": True,
        }

    def verify_identity(self, context: VerifiedContext) -> dict[str, str]:
        return {
            "account_id": context.trading_account_id,
            "portfolio_id": context.trading_portfolio_id,
        }

    def state_fingerprint(self, context: VerifiedContext) -> str:
        return self.fingerprint

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
    ) -> InstrumentSizing:
        return InstrumentSizing(
            instrument_id or 1001,
            strategy_notional_usd / leverage,
            strategy_notional_usd / Decimal("100"),
            strategy_notional_usd,
            Decimal("10"),
            Decimal("100"),
            "CFD",
        )

    def estimate_costs(self, *, context: VerifiedContext, sizing: InstrumentSizing, side: str) -> Decimal:
        return Decimal("2.00")

    def dispatch(self, context: VerifiedContext, mutation: BrokerMutation) -> BrokerOutcome:
        self.mutations.append(mutation)
        if mutation.operation == "open":
            return BrokerOutcome(
                state=self.outcome_state,
                request_id=mutation.request_id,
                broker_order_id="501",
                broker_position_id="601" if self.outcome_state is IntentState.FILLED else None,
                filled_units=Decimal("1"),
                actual_cost_usd=Decimal("1.50"),
                raw_fingerprint="response-hash",
            )
        if mutation.operation == "close":
            return BrokerOutcome(
                IntentState.FILLED,
                mutation.request_id,
                broker_order_id="502",
                realized_pnl_usd=Decimal("5"),
            )
        if mutation.operation == "modify":
            return BrokerOutcome(IntentState.ACKNOWLEDGED, mutation.request_id, broker_order_id="503")
        return BrokerOutcome(IntentState.CANCELED, mutation.request_id, broker_order_id=mutation.target_id)

    def lookup_request(self, context: VerifiedContext, request_id: str) -> BrokerOutcome | None:
        return None

    def lookup_order(self, context: VerifiedContext, order_id: str) -> BrokerOutcome | None:
        return None

    def lookup_close_order(self, context: VerifiedContext, order_id: str) -> BrokerOutcome | None:
        return None

    def reconcile(self, context: VerifiedContext) -> dict[str, object]:
        return {"accountId": context.trading_account_id, "portfolioId": context.trading_portfolio_id}


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    close_dbzero()
    profile_root = tmp_path / "profiles"
    profile_root.mkdir()
    monkeypatch.setattr(config_module, "PROJECT_ROOT", profile_root)
    profile = profile_root / ".env_test"
    profile.write_text(
        "\n".join(
            [
                "ETORO_API_KEY=api-secret",
                "ETORO_USER_KEY=child-secret",
                "TRADER_SERVICE_CREDENTIAL=service-secret",
                "ETORO_PNL_URL=https://public-api.etoro.com/api/v1/trading/info/demo/pnl",
                "ETORO_OPEN_ORDER_URL=https://public-api.etoro.com/api/v2/trading/execution/demo/orders",
                "ETORO_ORDER_LOOKUP_URL=https://public-api.etoro.com/api/v2/trading/info/demo/orders:lookup",
                "ETORO_CANCEL_ORDER_URL=https://public-api.etoro.com/api/v2/trading/execution/demo/orders/{orderId}",
                "ETORO_CLOSE_POSITION_URL=https://public-api.etoro.com/api/v1/trading/execution/demo/market-close-orders/positions/{positionId}",
                "ETORO_MODIFY_POSITION_URL=https://public-api.etoro.com/api/v2/trading/demo/positions/{positionId}",
                "ETORO_TRADING_COSTS_URL=https://public-api.etoro.com/api/v2/trading/info/demo/costs",
                "ETORO_TRADING_ELIGIBILITY_URL=https://public-api.etoro.com/api/v2/trading/info/demo/eligibility",
                "ETORO_MARKET_RATES_URL=https://public-api.etoro.com/api/v1/market-data/instruments/rates",
            ]
        )
        + "\n"
    )
    profile.chmod(0o600)
    suffix = uuid.uuid4().hex
    root = Path("/dbzero-data/trader-dev/tests") / suffix
    evidence = ScopeEvidence(
        frozenset({DEMO_READ, DEMO_WRITE}),
        "subject-alpha",
        "account-alpha",
        "portfolio-alpha",
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(hours=1),
    )
    admin = OwnerAdminService(
        environment=Environment.DEMO,
        owner_identity="owner",
        owner_service_credential="owner-secret",
        expected_owner_service_credential="owner-secret",
        storage_root=root,
    )
    admin.register_existing(
        trader_id="alpha",
        service_credential="service-secret",
        evidence=evidence,
        owner_account_id="owner-account",
        agent_portfolio_id="agent-uuid",
        agent_portfolio_gcid="agent-gcid",
        mirror_id="mirror-1",
        investment_usd="2000",
        virtual_balance_usd="10000",
        child_user_key_fingerprint=fingerprint("child-secret"),
    )
    yield {
        "root": root,
        "profile": ".env_test",
        "evidence": evidence,
        "verifier": StaticVerifier(evidence),
        "broker": FakeBroker(),
        "admin": admin,
    }
    close_dbzero()
    shutil.rmtree(root, ignore_errors=True)
