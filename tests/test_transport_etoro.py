from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from trader_api.broker.etoro import EtoroBrokerAdapter, open_payload
from trader_api.broker.transport import HttpTransport, RollingRateLimiter
from trader_api.config import load_profile
from trader_api.domain import BrokerMutation, Environment, IntentState, VerifiedContext
from trader_api.errors import TraderError


def _context() -> VerifiedContext:
    return VerifiedContext(
        Environment.DEMO,
        "subject",
        "account",
        "portfolio",
        "fingerprint",
        frozenset(),
        __import__("datetime").datetime.now(__import__("datetime").UTC),
        True,
        True,
    )


def test_http_transport_headers_rate_limit_and_malformed(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    transport = HttpTransport(profile, httpx.Client(transport=httpx.MockTransport(handler)))
    result = transport.request(profile.routes["ETORO_PNL_URL"], request_id="request-id")
    assert result == {"ok": True}
    assert seen["x-api-key"] == "api-secret"
    assert seen["x-user-key"] == "child-secret"
    assert seen["x-request-id"] == "request-id"

    for status, code in ((429, "BROKER_RATE_LIMITED"), (403, "PERMISSION_REVOKED")):

        def respond(_request: httpx.Request, response_status: int = status) -> httpx.Response:
            return httpx.Response(response_status)

        failed = HttpTransport(
            profile,
            httpx.Client(transport=httpx.MockTransport(respond)),
        )
        with pytest.raises(TraderError) as raised:
            failed.request(profile.routes["ETORO_PNL_URL"])
        assert raised.value.code == code

    malformed = HttpTransport(
        profile,
        httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="no"))),
    )
    with pytest.raises(TraderError) as raised:
        malformed.request(profile.routes["ETORO_PNL_URL"])
    assert raised.value.code == "BROKER_PROTOCOL_ERROR"


def test_etoro_contract_dispatch_lookup_and_identity(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))

    class FixtureTransport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def request(self, route: object, **kwargs: object) -> dict[str, object]:
            name = route.name  # type: ignore[attr-defined]
            self.calls.append((name, kwargs))
            if name == "ETORO_PNL_URL":
                return {"accountId": "account", "portfolioId": "portfolio", "equity": "10"}
            if name == "ETORO_ORDER_LOOKUP_URL":
                return {
                    "status": {"id": 3, "name": "Filled", "errorCode": 0, "errorMessage": ""},
                    "orderId": 77,
                    "positionExecutions": [{"positionId": 88, "remainingUnits": "2"}],
                    "totalCosts": "1.50",
                }
            return {"status": "pending", "orderId": 77}

    transport = FixtureTransport()
    adapter = EtoroBrokerAdapter(profile, transport)  # type: ignore[arg-type]
    assert adapter.verify_identity(_context())["account_id"] == "account"
    assert len(adapter.state_fingerprint(_context())) == 64
    outcome = adapter.dispatch(_context(), BrokerMutation("req", "open", {"action": "open"}))
    assert outcome.state == "ACKNOWLEDGED"
    recovered = adapter.lookup_request(_context(), "req")
    assert recovered is not None and recovered.broker_position_id == "88"


def test_order_type_specific_validation() -> None:
    from trader_api.broker.base import InstrumentSizing

    sizing = InstrumentSizing(1, Decimal("100"), Decimal("1"), Decimal("100"), Decimal("10"), Decimal("100"), "CFD")
    with pytest.raises(TraderError, match="trigger_rate"):
        open_payload(
            sizing=sizing,
            side="long",
            order_type="market_if_touched",
            leverage=1,
            trigger_rate=None,
            limit_rate=None,
            stop_loss_rate=None,
            take_profit_rate=None,
        )
    with pytest.raises(TraderError, match="within 10%"):
        open_payload(
            sizing=sizing,
            side="long",
            order_type="limit_ioc",
            leverage=1,
            trigger_rate=None,
            limit_rate=Decimal("120"),
            stop_loss_rate=None,
            take_profit_rate=None,
        )


def test_etoro_eligibility_and_costs_are_normalized(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))

    class SizingTransport:
        def request(self, route: object, **kwargs: object) -> dict[str, object]:
            if route.name == "ETORO_TRADING_ELIGIBILITY_URL":  # type: ignore[attr-defined]
                return {
                    "eligibilities": [
                        {
                            "instrumentId": 42,
                            "allowOpenPosition": True,
                            "allowMitOrders": True,
                            "minPositionExposure": "10",
                            "leverageConfigs": [
                                {
                                    "direction": "long",
                                    "leverageValues": [2],
                                    "settlementType": "CFD",
                                    "minPositionAmount": "10",
                                }
                            ],
                        }
                    ]
                }
            if route.name == "ETORO_MARKET_RATES_URL":  # type: ignore[attr-defined]
                return {"rates": [{"instrumentID": 42, "ask": "25", "bid": "24"}]}
            return {"costs": [{"costType": "fee", "value": "1.23", "currency": "usd"}]}

    adapter = EtoroBrokerAdapter(profile, SizingTransport())  # type: ignore[arg-type]
    assert adapter.capabilities()["market"] is True
    sizing = adapter.resolve_sizing(
        context=_context(),
        symbol="AAPL",
        instrument_id=None,
        strategy_notional_usd=Decimal("100"),
        leverage=2,
        order_type="market",
        side="long",
    )
    assert sizing.amount_usd == Decimal("50.00")
    assert sizing.units == Decimal("4.000000")
    assert adapter.estimate_costs(context=_context(), sizing=sizing, side="long") == Decimal("1.23")


def test_identity_mismatch_and_malformed_mutation_are_rejected(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))

    class BadTransport:
        def request(self, route: object, **kwargs: object) -> dict[str, object]:
            if route.name == "ETORO_PNL_URL":  # type: ignore[attr-defined]
                return {"accountId": "wrong", "portfolioId": "portfolio"}
            return {"status": "surprise"}

    adapter = EtoroBrokerAdapter(profile, BadTransport())  # type: ignore[arg-type]
    with pytest.raises(TraderError) as identity:
        adapter.verify_identity(_context())
    assert identity.value.code == "ACCOUNT_MISMATCH"
    with pytest.raises(TraderError) as malformed:
        adapter.dispatch(_context(), BrokerMutation("req", "open", {}))
    assert malformed.value.code == "BROKER_PROTOCOL_ERROR"


def test_identity_accepts_documented_pnl_schema_with_control_binding(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))

    class PnlTransport:
        def request(self, route: object, **kwargs: object) -> dict[str, object]:
            return {"clientPortfolio": {"credit": 10000}}

    adapter = EtoroBrokerAdapter(profile, PnlTransport())  # type: ignore[arg-type]
    assert adapter.verify_identity(_context()) == {"account_id": "account", "portfolio_id": "portfolio"}


def test_rate_limiter_reserves_polling_capacity() -> None:
    limiter = RollingRateLimiter(clock=lambda: 100.0)
    for _ in range(16):
        limiter.acquire(mutation=True)
    with pytest.raises(TraderError) as limited:
        limiter.acquire(mutation=True)
    assert limited.value.code == "BROKER_RATE_LIMITED"
    for _ in range(4):
        limiter.acquire(mutation=False)
    with pytest.raises(TraderError):
        limiter.acquire(mutation=False)


def test_close_submission_nested_contract(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))

    class CloseTransport:
        def request(self, route: object, **kwargs: object) -> dict[str, object]:
            return {
                "orderForClose": {
                    "positionID": 601,
                    "instrumentID": 1001,
                    "unitsToDeduct": "0.5",
                    "orderID": 888,
                    "statusID": 1,
                },
                "token": "confirmation-token",
            }

    adapter = EtoroBrokerAdapter(profile, CloseTransport())  # type: ignore[arg-type]
    outcome = adapter.dispatch(_context(), BrokerMutation("req", "close", {"InstrumentID": 1001}, "601"))
    assert outcome.state is IntentState.ACKNOWLEDGED
    assert outcome.broker_order_id == "888"
    assert outcome.broker_position_id == "601"


def test_confirmed_execution_prices_use_opening_units_not_remaining_units():
    from trader_api.broker.etoro import _outcome

    outcome = _outcome(
        {
            "orderId": 77,
            "status": {"id": 3},
            "totalCosts": "1.50",
            "positionExecutions": [
                {"positionId": 88, "remainingUnits": "0.5", "openingData": {"units": "2", "avgPrice": "100"}}
            ],
        },
        "req",
    )
    assert outcome.filled_units == Decimal(2)
    assert outcome.execution_price == Decimal(100)
    assert outcome.actual_cost_usd == Decimal("1.50")
    missing = _outcome(
        {"orderId": 77, "status": {"id": 3}, "positionExecutions": [{"positionId": 88, "remainingUnits": "0.5"}]}, "req"
    )
    assert missing.execution_price is None and missing.filled_units is None
    closed = _outcome(
        {"orderID": 99, "statusID": 3, "positions": [{"positionID": 88, "units": "0.5", "rate": "110"}]}, "req-close"
    )
    assert closed.execution_price == Decimal(110) and closed.filled_units == Decimal("0.5")
    assert closed.actual_cost_usd is None
