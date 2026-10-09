from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trader_api.broker.etoro import EtoroBrokerAdapter
from trader_api.domain import BrokerOutcome, Environment, IntentState
from trader_api.errors import TraderError
from trader_api.service import TraderService
from trader_api.storage import DirectAccount, LedgerDomain, LedgerEntry, PortfolioBinding, Reservation
from trader_api.ui_api import get_dashboard, open_session

from .conftest import FakeBroker


class DirectBroker(FakeBroker):
    def probe_direct(self, environment: Environment) -> dict[str, str]:
        assert environment is Environment.DEMO
        return {"account_id": "direct-account", "portfolio_id": "direct-portfolio"}


def direct_service(runtime: dict[str, object], broker: DirectBroker | None = None) -> TraderService:
    from trader_api.config import PROJECT_ROOT

    profile = Path(PROJECT_ROOT) / str(runtime["profile"])
    if "TRADER_TRADING_MODE" not in profile.read_text():
        profile.write_text(profile.read_text() + "\nTRADER_TRADING_MODE=standalone\n")
    return TraderService(
        "direct",
        str(runtime["profile"]),
        "demo",
        broker=broker or DirectBroker(),
        storage_root=runtime["root"],
    )


def test_direct_startup_initialization_preview_and_single_dispatch(runtime: dict[str, object]) -> None:
    broker = DirectBroker()
    service = direct_service(runtime, broker)
    assert service.trader_status()["lifecycle"] == "READY"
    assert service.capabilities()["direct_account_verified"]
    assert service.store is not None and service.prefix is not None
    assert service.store.binding(service.prefix).binding_version == 0
    assert service.initialize("1000")["strategy_budget"]["initial_policy_cap_usd"] == "1000.00"
    assert service.initialize("1000")["owner_mirror_budget"]["initial_policy_cap_usd"] == "0.00"
    preview = service.preview_open(symbol="ETH", side="long", strategy_notional_usd="100")
    assert preview["owner_reservation_usd"] == "0.00"
    assert preview["strategy_reservation_usd"] == "102.00"
    outcome = service.submit(preview["preview_id"], "direct-once")
    assert outcome["state"] == "FILLED"
    assert outcome["mirror_reconciliation_state"] == "NOT_APPLICABLE"
    assert service.submit(preview["preview_id"], "direct-once") == outcome
    assert len(broker.mutations) == 1
    assert service.trader_status()["strategy_budget"]["committed_usd"] == "100.00"
    assert service.store.one(Reservation, prefix=service.prefix).owner_amount_usd == Decimal("0.00")
    assert all(
        entry.domain == LedgerDomain.strategy
        for entry in service.store.all(LedgerEntry, "LEDGER", prefix=service.prefix)
    )
    service.store.open(service.prefix)
    dashboard = get_dashboard(open_session(service))
    assert str(dashboard.lifecycle) == "ACTIVE"
    assert dashboard.trading_mode == "standalone" and dashboard.direct_account_verified
    assert dashboard.owner_allocation == 0


@pytest.mark.parametrize("state", [IntentState.ACKNOWLEDGED, IntentState.REJECTED, IntentState.UNKNOWN])
def test_direct_outcomes_and_recovery(runtime: dict[str, object], state: IntentState) -> None:
    broker = DirectBroker()
    broker.outcome_state = state
    service = direct_service(runtime, broker)
    service.initialize("1000")
    preview = service.preview_open(symbol="ETH", side="long", strategy_notional_usd="100")
    outcome = service.submit(preview["preview_id"], "once")
    assert outcome["state"] == state.value
    assert len(broker.mutations) == 1
    if state is IntentState.UNKNOWN:
        broker.lookup_request = lambda _context, request: BrokerOutcome(
            IntentState.FILLED,
            request,
            broker_order_id="501",
            broker_position_id="601",
        )
        assert service.reconcile()["mirror"] == "not_applicable"
        assert service.intent_status(outcome["intent_id"])["state"] == "FILLED"
    elif state is IntentState.REJECTED:
        assert service.trader_status()["strategy_budget"]["committed_usd"] == "0.00"


def test_smoke_is_durable_and_refuses_bound_mode(runtime: dict[str, object]) -> None:
    bound = TraderService(
        "alpha",
        str(runtime["profile"]),
        "demo",
        scope_verifier=runtime["verifier"],
        broker=runtime["broker"],
        storage_root=runtime["root"],
    )
    with pytest.raises(TraderError) as raised:
        bound.smoke_demo("1000")
    assert raised.value.code == "DEMO_ONLY"
    broker = DirectBroker()
    service = direct_service(runtime, broker)
    first = service.smoke_demo("1000", "ETH", "100")
    second = service.smoke_demo("1000", "ETH", "100")
    assert first["outcome"] == second["outcome"]
    assert first["preview"] is not None and second["preview"] is None
    assert first["preview"]["symbol"] == "ETH"
    assert len(broker.mutations) == 1
    assert service.store is not None and service.prefix is not None
    assert service.store.one(DirectAccount, prefix=service.prefix).account_id == "direct-account"
    assert service.store.one(PortfolioBinding, prefix=service.prefix).binding_version == 0


def test_direct_account_cannot_switch_identity(runtime: dict[str, object]) -> None:
    direct_service(runtime)

    class DifferentAccount(DirectBroker):
        def probe_direct(self, environment: Environment) -> dict[str, str]:
            return {"account_id": "other-account", "portfolio_id": "direct-portfolio"}

    with pytest.raises(TraderError) as raised:
        direct_service(runtime, DifferentAccount())
    assert raised.value.code == "ACCOUNT_MISMATCH"


def test_direct_probe_uses_authenticated_credential_identity_for_documented_pnl_shape(
    runtime: dict[str, object],
) -> None:
    class Transport:
        def request(self, route: object, **_kwargs: object) -> dict[str, object]:
            if route.name == "ETORO_PNL_URL":  # type: ignore[attr-defined]
                return {"clientPortfolio": {"credit": "1000"}}
            return {"eligibilities": [{"allowOpenPosition": True}]}

    from trader_api.config import load_profile

    broker = EtoroBrokerAdapter(load_profile(str(runtime["profile"])), Transport())  # type: ignore[arg-type]
    identity = broker.probe_direct(Environment.DEMO)
    assert identity["account_id"].startswith("credential:")
    assert identity["portfolio_id"].startswith("direct:demo:")
    assert identity["identity_source"] == "authenticated_user_key"


def test_direct_probe_checks_environment_and_eligibility(runtime: dict[str, object]) -> None:
    from trader_api.config import load_profile

    calls: list[str] = []

    class Transport:
        def request(self, route: object, **_kwargs: object) -> dict[str, object]:
            calls.append(route.name)  # type: ignore[attr-defined]
            if route.name == "ETORO_PNL_URL":  # type: ignore[attr-defined]
                return {"accountId": "direct-account", "portfolioId": "direct-portfolio"}
            return {"eligibilities": [{"allowOpenPosition": True}]}

    broker = EtoroBrokerAdapter(load_profile(str(runtime["profile"])), Transport())  # type: ignore[arg-type]
    with pytest.raises(TraderError) as raised:
        broker.probe_direct(Environment.REAL)
    assert raised.value.code == "STANDALONE_ROUTES_MISSING"
    assert not calls
    assert broker.probe_direct(Environment.DEMO)["account_id"] == "direct-account"
    assert calls == ["ETORO_PNL_URL", "ETORO_TRADING_ELIGIBILITY_URL"]
