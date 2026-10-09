from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..config import Profile, route_environment
from ..domain import BrokerMutation, BrokerOutcome, Environment, IntentState, VerifiedContext, decimal_value, money
from ..errors import TraderError
from .base import InstrumentSizing
from .observations import PortfolioObservation, PriceQuote, normalize_portfolio
from .transport import HttpTransport


class EtoroBrokerAdapter:
    """Strict v2 adapter. Capabilities remain off when a required route is absent."""

    def __init__(self, profile: Profile, transport: HttpTransport | None = None) -> None:
        self.profile = profile
        self.transport = transport or HttpTransport(profile)

    def capabilities(self) -> dict[str, bool]:
        present = self.profile.routes.__contains__
        return {
            "market": present("ETORO_OPEN_ORDER_URL") and present("ETORO_TRADING_ELIGIBILITY_URL"),
            "market_if_touched": present("ETORO_OPEN_ORDER_URL") and present("ETORO_TRADING_ELIGIBILITY_URL"),
            "limit_ioc": present("ETORO_OPEN_ORDER_URL") and present("ETORO_TRADING_ELIGIBILITY_URL"),
            "cancel": present("ETORO_CANCEL_ORDER_URL"),
            "cancel_close": present("ETORO_CANCEL_CLOSE_ORDER_URL"),
            "partial_close": present("ETORO_CLOSE_POSITION_URL"),
            "modify_protection": present("ETORO_MODIFY_POSITION_URL"),
            "request_lookup": present("ETORO_ORDER_LOOKUP_URL"),
        }

    def probe_direct(self, environment: Environment) -> dict[str, str]:
        """Authenticate the direct account without any Agent Portfolio metadata.

        Ordinary-account P&L responses currently omit numeric account/portfolio IDs.
        In that documented shape, the authenticated user key is the stable account
        identity and is persisted only as a one-way local fingerprint.
        """
        route = self.profile.route("ETORO_PNL_URL")
        eligibility = self.profile.route("ETORO_TRADING_ELIGIBILITY_URL")
        if (
            route is None
            or eligibility is None
            or route.environment is not environment
            or eligibility.environment is not environment
        ):
            raise TraderError("STANDALONE_ROUTES_MISSING", "direct identity and eligibility routes are required")
        if (
            not self.capabilities()["market"]
            or self.profile.route("ETORO_TRADING_COSTS_URL") is None
            or self.profile.route("ETORO_MARKET_RATES_URL") is None
        ):
            raise TraderError("STANDALONE_ROUTES_MISSING", "direct opening, quote and cost routes are required")
        payload = self.transport.request(route)
        body = payload.get("clientPortfolio", payload)
        if not isinstance(body, dict):
            raise TraderError("DIRECT_ACCOUNT_IDENTITY_UNVERIFIED", "broker account identity is unavailable")
        account = payload.get("accountId", payload.get("accountID", body.get("accountId", body.get("accountID"))))
        portfolio = payload.get(
            "portfolioId", payload.get("portfolioID", body.get("portfolioId", body.get("portfolioID")))
        )
        if bool(account) != bool(portfolio):
            raise TraderError("BROKER_PROTOCOL_ERROR", "broker response has a partial direct-account identity")
        probe = self.transport.request(eligibility, json_body={"currency": "USD", "symbols": ["ETH"]})
        choices = probe.get("eligibilities")
        if not isinstance(choices, list):
            raise TraderError("BROKER_PROTOCOL_ERROR", "eligibility response is invalid")
        if not choices or not isinstance(choices[0], dict) or not choices[0].get("allowOpenPosition"):
            raise TraderError("UNSUPPORTED_CAPABILITY", "ETH opening is not eligible for this account")
        if account and portfolio:
            return {
                "account_id": str(account),
                "portfolio_id": str(portfolio),
                "identity_source": "broker_fields",
            }
        credential_id = hashlib.sha256(self.profile.user_key.encode()).hexdigest()[:16]
        return {
            "account_id": f"credential:{credential_id}",
            "portfolio_id": f"direct:{environment.value}:{credential_id}",
            "identity_source": "authenticated_user_key",
        }

    def verify_identity(self, context: VerifiedContext) -> dict[str, str]:
        route = self.profile.route("ETORO_PNL_URL")
        if route is None:
            raise TraderError("CONFIG_INVALID", "identity read route is missing")
        payload = self.transport.request(route)
        account_id = str(payload.get("accountId", payload.get("accountID", "")))
        portfolio_id = str(payload.get("portfolioId", payload.get("portfolioID", "")))
        if not account_id and not portfolio_id:
            # The current PnL schema authenticates the exact child token but
            # exposes only clientPortfolio. Its immutable portfolio/GCID
            # binding therefore comes from the owner-retained create response,
            # indexed by this token's fingerprint in ControlScopeVerifier.
            if not isinstance(payload.get("clientPortfolio"), dict):
                raise TraderError("BROKER_PROTOCOL_ERROR", "identity response lacks portfolio data")
            return {
                "account_id": context.trading_account_id,
                "portfolio_id": context.trading_portfolio_id,
            }
        if not account_id or not portfolio_id:
            raise TraderError("BROKER_PROTOCOL_ERROR", "identity response has a partial account binding")
        if account_id != context.trading_account_id or portfolio_id != context.trading_portfolio_id:
            raise TraderError("ACCOUNT_MISMATCH", "broker identity does not match verified token metadata")
        return {"account_id": account_id, "portfolio_id": portfolio_id}

    def state_fingerprint(self, context: VerifiedContext) -> str:
        route = self.profile.route("ETORO_PNL_URL")
        if route is None:
            raise TraderError("CONFIG_INVALID", "state read route is missing")
        payload = self.transport.request(route)
        client = payload.get("clientPortfolio")
        if isinstance(client, dict):
            stable: dict[str, object] = {"credit": client.get("credit")}
            for name in (
                "positions",
                "orders",
                "ordersForOpen",
                "ordersForClose",
                "ordersForCloseMultiple",
            ):
                items = client.get(name, [])
                if isinstance(items, list):
                    stable[name] = sorted(
                        (
                            {
                                key: item.get(key)
                                for key in (
                                    "positionID",
                                    "positionId",
                                    "orderID",
                                    "orderId",
                                    "instrumentID",
                                    "instrumentId",
                                    "amount",
                                    "amountInUnits",
                                    "unitsToDeduct",
                                    "rate",
                                    "statusID",
                                    "statusId",
                                )
                                if key in item
                            }
                            for item in items
                            if isinstance(item, dict)
                        ),
                        key=lambda item: json.dumps(item, sort_keys=True, default=str),
                    )
                else:
                    stable[name] = None
            payload = stable
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest()

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
        if (symbol is None) == (instrument_id is None):
            raise TraderError("INVALID_INSTRUMENT", "provide exactly one symbol or instrument ID")
        route = self.profile.route("ETORO_TRADING_ELIGIBILITY_URL")
        if route is None:
            raise TraderError("UNSUPPORTED_CAPABILITY", "sizing eligibility is not configured")
        body: dict[str, Any] = {"currency": "USD"}
        body["symbols" if symbol is not None else "instrumentIds"] = [symbol if symbol is not None else instrument_id]
        payload = self.transport.request(route, json_body=body)
        try:
            eligibilities = payload["eligibilities"]
            if not isinstance(eligibilities, list) or len(eligibilities) != 1:
                raise ValueError
            eligibility = eligibilities[0]
            resolved_id = int(eligibility["instrumentId"])
            if not bool(eligibility["allowOpenPosition"]):
                raise TraderError("UNSUPPORTED_CAPABILITY", "instrument is not open for new positions")
            if order_type == "market_if_touched" and not bool(eligibility["allowMitOrders"]):
                raise TraderError("UNSUPPORTED_CAPABILITY", "instrument does not allow MIT orders")
            direction = "long" if side == "long" else "short"
            configs = [
                item
                for item in eligibility["leverageConfigs"]
                if str(item.get("direction", "")).lower() == direction
                and leverage in [int(value) for value in item.get("leverageValues", [])]
            ]
            if len(configs) != 1:
                raise TraderError("UNSUPPORTED_CAPABILITY", "direction and leverage are not eligible")
            settlement = str(configs[0]["settlementType"]).lower()
            minimum = money(
                max(decimal_value(eligibility["minPositionExposure"]), decimal_value(configs[0]["minPositionAmount"]))
            )
        except TraderError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "eligibility response lacks sizing fields") from exc
        market_route = self.profile.route("ETORO_MARKET_RATES_URL")
        if market_route is None:
            raise TraderError("UNSUPPORTED_CAPABILITY", "market-rate lookup is not configured")
        rates_payload = self.transport.request(market_route, params={"instrumentIds": str(resolved_id)})
        try:
            rates = rates_payload["rates"]
            if not isinstance(rates, list) or len(rates) != 1 or int(rates[0]["instrumentID"]) != resolved_id:
                raise ValueError
            price = decimal_value(rates[0]["ask" if side == "long" else "bid"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "market-rate response lacks the requested quote") from exc
        if strategy_notional_usd <= 0 or price <= 0 or leverage < 1:
            raise TraderError("INVALID_AMOUNT", "notional, quote, and leverage must be positive")
        amount = money(strategy_notional_usd / Decimal(leverage))
        if amount < minimum:
            raise TraderError("BROKER_MINIMUM", "trade amount is below the broker minimum")
        units = (strategy_notional_usd / price).quantize(Decimal("0.000001"))
        return InstrumentSizing(resolved_id, amount, units, money(strategy_notional_usd), minimum, price, settlement)

    def quote(self, context: VerifiedContext, instrument_id: int, observed_at: datetime) -> PriceQuote:
        route = self.profile.route("ETORO_MARKET_RATES_URL")
        if route is None:
            raise TraderError("VALUATION_UNAVAILABLE", "market-rate lookup is not configured")
        payload = self.transport.request(route, params={"instrumentIds": str(instrument_id)})
        try:
            rates = payload["rates"]
            matching = [r for r in rates if int(r["instrumentID"]) == instrument_id]
            if len(matching) != 1:
                raise ValueError
            bid, ask = decimal_value(matching[0]["bid"]), decimal_value(matching[0]["ask"])
            if bid <= 0 or ask < bid:
                raise ValueError
            return PriceQuote(instrument_id, bid, ask, observed_at)
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError("VALUATION_UNAVAILABLE", "requested quote is unavailable") from exc

    def estimate_costs(self, *, context: VerifiedContext, sizing: InstrumentSizing, side: str) -> Decimal:
        route = self.profile.route("ETORO_TRADING_COSTS_URL")
        if route is None:
            raise TraderError("UNSUPPORTED_CAPABILITY", "cost estimation is not configured")
        payload = self.transport.request(
            route,
            json_body={
                "action": "open",
                "transaction": "buy" if side == "long" else "sellShort",
                "instrumentId": sizing.instrument_id,
                "settlementType": sizing.settlement_type,
                "orderType": "mkt",
                "amount": str(sizing.amount_usd),
                "leverage": int(sizing.full_notional_usd / sizing.amount_usd),
                "orderCurrency": "usd",
            },
        )
        try:
            raw_costs = payload["costs"]
            if not isinstance(raw_costs, list):
                raise ValueError
            total = sum(
                (decimal_value(item["value"] if "value" in item else item["amount"]) for item in raw_costs),
                Decimal("0"),
            )
            return money(total)
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "cost response lacks cost components") from exc

    def dispatch(self, context: VerifiedContext, mutation: BrokerMutation) -> BrokerOutcome:
        route_name, path_key = {
            "open": ("ETORO_OPEN_ORDER_URL", None),
            "cancel": ("ETORO_CANCEL_ORDER_URL", "orderId"),
            "cancel_close": ("ETORO_CANCEL_CLOSE_ORDER_URL", "orderId"),
            "close": ("ETORO_CLOSE_POSITION_URL", "positionId"),
            "modify": ("ETORO_MODIFY_POSITION_URL", "positionId"),
        }.get(mutation.operation, ("", None))
        route = self.profile.route(route_name)
        if route is None:
            raise TraderError("UNSUPPORTED_CAPABILITY", "broker operation is not configured")
        path_values = None
        if path_key is not None:
            try:
                path_values = {path_key: int(mutation.target_id or "")}
            except ValueError as exc:
                raise TraderError("INVALID_BROKER_ID", "broker entity ID must be numeric") from exc
        payload = self.transport.request(
            route,
            request_id=mutation.request_id,
            path_values=path_values,
            json_body=mutation.payload or None,
        )
        return _outcome(payload, mutation.request_id)

    def lookup_request(self, context: VerifiedContext, request_id: str) -> BrokerOutcome | None:
        route = self.profile.route("ETORO_ORDER_LOOKUP_URL")
        if route is None:
            return None
        try:
            payload = self.transport.request(route, params={"referenceId": request_id})
        except TraderError as exc:
            if exc.code == "BROKER_REJECTED" and exc.details.get("status_code") == 404:
                return None  # A recently accepted order can briefly be absent from the lookup index.
            raise
        if not payload:
            return None
        return _outcome(payload, request_id)

    def lookup_order(self, context: VerifiedContext, order_id: str) -> BrokerOutcome | None:
        route = self.profile.route("ETORO_ORDER_LOOKUP_URL")
        if route is None:
            return None
        if not order_id.isdigit():
            raise TraderError("INVALID_BROKER_ID", "broker order ID must be numeric")
        try:
            payload = self.transport.request(route, params={"orderId": order_id})
        except TraderError as exc:
            if exc.code == "BROKER_REJECTED" and exc.details.get("status_code") == 404:
                return None
            raise
        if not payload:
            return None
        return _outcome(payload, str(payload.get("referenceId") or order_id))

    def lookup_close_order(self, context: VerifiedContext, order_id: str) -> BrokerOutcome | None:
        route = self.profile.route("ETORO_CLOSE_ORDER_LOOKUP_URL")
        if route is None:
            return None
        if not order_id.isdigit():
            raise TraderError("INVALID_BROKER_ID", "broker close-order ID must be numeric")
        payload = self.transport.request(route, path_values={"orderId": int(order_id)})
        if not payload:
            return None
        return _outcome(payload, str(payload.get("referenceID") or order_id))

    def reconcile(self, context: VerifiedContext) -> dict[str, object]:
        route = self.profile.route("ETORO_PNL_URL")
        if route is None:
            raise TraderError("CONFIG_INVALID", "reconciliation route is missing")
        return self.transport.request(route)

    def collect_portfolio(self, context: VerifiedContext, observed_at: datetime) -> PortfolioObservation:
        """Read only the bound child portfolio; do not run execution reconciliation."""
        if not context.can_read or context.environment != route_environment(self.profile):
            raise TraderError("PERMISSION_REQUIRED", "verified environment read scope is required")
        route = self.profile.route("ETORO_PNL_URL")
        if route is None:
            raise TraderError("CONFIG_INVALID", "portfolio read route is missing")
        return normalize_portfolio(self.transport.request(route), context, observed_at)


def _order_type(value: str) -> str:
    try:
        return {"market": "mkt", "market_if_touched": "mit", "limit_ioc": "limitIOC"}[value]
    except KeyError as exc:
        raise TraderError("UNSUPPORTED_CAPABILITY", "unsupported order type") from exc


def open_payload(
    *,
    sizing: InstrumentSizing,
    side: str,
    order_type: str,
    leverage: int,
    trigger_rate: Decimal | None,
    limit_rate: Decimal | None,
    stop_loss_rate: Decimal | None,
    take_profit_rate: Decimal | None,
) -> dict[str, object]:
    if side not in {"long", "short"}:
        raise TraderError("INVALID_SIDE", "side must be long or short")
    mapped_type = _order_type(order_type)
    if mapped_type == "mit" and (trigger_rate is None or trigger_rate <= 0):
        raise TraderError("INVALID_ORDER", "market-if-touched requires trigger_rate")
    if mapped_type == "limitIOC":
        if limit_rate is None or limit_rate <= 0:
            raise TraderError("INVALID_ORDER", "limit_ioc requires a positive limit_rate")
        deviation = abs(limit_rate - sizing.market_price) / sizing.market_price
        if deviation > Decimal("0.10"):
            raise TraderError("INVALID_ORDER", "limit_rate must be within 10% of market price")
    elif limit_rate is not None:
        raise TraderError("INVALID_ORDER", "limit_rate is only valid for limit_ioc")
    if leverage > 1 and stop_loss_rate is None:
        raise TraderError("INVALID_ORDER", "leveraged opening requires a stop-loss rate")
    payload: dict[str, object] = {
        "action": "open",
        "transaction": "buy" if side == "long" else "sellShort",
        "orderType": mapped_type,
        "instrumentId": sizing.instrument_id,
        "settlementType": sizing.settlement_type,
        "amount": str(sizing.amount_usd),
        "leverage": leverage,
        "orderCurrency": "usd",
    }
    for key, value in (
        ("triggerRate", trigger_rate),
        ("limitRate", limit_rate),
        ("stopLossRate", stop_loss_rate),
        ("takeProfitRate", take_profit_rate),
    ):
        if value is not None:
            payload[key] = str(value)
    return payload


def _outcome(payload: dict[str, Any], request_id: str) -> BrokerOutcome:
    close_submission = payload.get("orderForClose")
    if isinstance(close_submission, dict):
        payload = {
            **payload,
            "orderId": close_submission.get("orderID"),
            "positionId": close_submission.get("positionID"),
            "status": {"id": close_submission.get("statusID")},
            "units": close_submission.get("unitsToDeduct"),
        }
    elif "statusID" in payload and "orderID" in payload:
        positions = payload.get("positions", [])
        position_id = None
        if isinstance(positions, list) and positions and isinstance(positions[0], dict):
            position_id = positions[0].get("positionID")
        payload = {
            **payload,
            "orderId": payload.get("orderID"),
            "positionId": position_id,
            "status": {"id": payload.get("statusID")},
        }
    order_id = payload.get("orderId")
    position_id = payload.get("positionId")
    raw_status = payload.get("status", "pending")
    executions = payload.get("positionExecutions", [])
    if isinstance(raw_status, dict):
        try:
            status_id = int(raw_status["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "broker order status is malformed") from exc
        state = {
            1: IntentState.ACKNOWLEDGED,
            2: IntentState.ACKNOWLEDGED,
            3: IntentState.FILLED,
            4: IntentState.REJECTED,
            5: IntentState.ACKNOWLEDGED,
            6: IntentState.ACKNOWLEDGED,
            7: IntentState.CANCELED,
            8: IntentState.CANCELED,
            11: IntentState.ACKNOWLEDGED,
            12: IntentState.ACKNOWLEDGED,
        }.get(status_id)
        if status_id in {9, 10}:
            state = IntentState.FILLED if executions else IntentState.CANCELED
    else:
        status = str(raw_status).lower()
        state = {
            "filled": IntentState.FILLED,
            "executed": IntentState.FILLED,
            "rejected": IntentState.REJECTED,
            "canceled": IntentState.CANCELED,
            "pending": IntentState.ACKNOWLEDGED,
            "accepted": IntentState.ACKNOWLEDGED,
        }.get(status)
    if position_id is None and isinstance(executions, list) and executions and isinstance(executions[0], dict):
        position_id = executions[0].get("positionId")
    filled_units: Decimal | None = None
    execution_price: Decimal | None = None
    if isinstance(executions, list) and executions:
        try:
            if len({str(item["positionId"]) for item in executions}) != 1:
                raise TraderError("BROKER_OUTCOME_UNKNOWN", "multiple execution positions require reconciliation")
            opening = [item.get("openingData") for item in executions]
            if all(isinstance(item, dict) and item.get("units") is not None for item in opening):
                filled_units = sum((decimal_value(item["units"]) for item in opening), Decimal(0))
                if filled_units > 0 and all(item.get("avgPrice") is not None for item in opening):
                    execution_price = (
                        sum(
                            (decimal_value(item["units"]) * decimal_value(item["avgPrice"]) for item in opening),
                            Decimal(0),
                        )
                        / filled_units
                    )
        except (KeyError, TypeError, ValueError) as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "broker execution details are malformed") from exc
    closed_positions = payload.get("positions")
    if isinstance(closed_positions, list) and closed_positions and state is IntentState.FILLED:
        if len({str(item.get("positionID")) for item in closed_positions}) != 1:
            raise TraderError("BROKER_OUTCOME_UNKNOWN", "multiple closed positions require reconciliation")
        if all(item.get("units") is not None for item in closed_positions):
            filled_units = sum((decimal_value(item["units"]) for item in closed_positions), Decimal(0))
            if filled_units > 0 and all(item.get("rate") is not None for item in closed_positions):
                execution_price = (
                    sum(
                        (decimal_value(item["units"]) * decimal_value(item["rate"]) for item in closed_positions),
                        Decimal(0),
                    )
                    / filled_units
                )
    if state is None or (order_id is None and position_id is None and state is not IntentState.REJECTED):
        raise TraderError("BROKER_PROTOCOL_ERROR", "broker mutation response is malformed")
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return BrokerOutcome(
        state=state,
        request_id=request_id,
        broker_order_id=None if order_id is None else str(order_id),
        broker_position_id=None if position_id is None else str(position_id),
        filled_units=filled_units
        if filled_units is not None
        else (None if payload.get("units") is None else decimal_value(payload["units"])),
        actual_cost_usd=(
            money(payload["totalCosts"])
            if payload.get("totalCosts") is not None
            else (None if payload.get("costInUsd") is None else money(payload["costInUsd"]))
        ),
        realized_pnl_usd=None
        if payload.get("realizedPnlInUsd") is None
        else money(payload["realizedPnlInUsd"], allow_negative=True),
        raw_fingerprint=fingerprint,
        execution_price=(
            execution_price if payload.get("executionPrice") is None else decimal_value(payload["executionPrice"])
        ),
    )
