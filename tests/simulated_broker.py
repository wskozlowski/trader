"""Deterministic shared-account broker, never connects to an external service."""

from dataclasses import replace
from decimal import Decimal

from trader_api.broker.observations import PriceQuote
from trader_api.domain import BrokerOutcome, IntentState

from .test_standalone import DirectBroker


class SimulatedBroker(DirectBroker):
    def __init__(self):
        super().__init__()
        self.positions = {"external": (Decimal(100), "long", 9999)}
        self.outcomes = {}
        self.prices = {}
        self.price_errors = set()
        self.queries = []
        self.lookups = []
        self.next_id = 1000

    def dispatch(self, context, mutation):
        self.mutations.append(mutation)
        self.next_id += 1
        order = str(self.next_id)
        if mutation.operation == "open":
            pending = mutation.payload["orderType"] == "mit"
            units = Decimal(mutation.payload["amount"]) * mutation.payload["leverage"] / 100
            position = "p" + order
            if not pending:
                self.positions[position] = (units, mutation.payload["transaction"], mutation.payload["instrumentId"])
            outcome = BrokerOutcome(
                IntentState.ACKNOWLEDGED if pending else IntentState.FILLED,
                mutation.request_id,
                order,
                None if pending else position,
                units if not pending else None,
                Decimal("1") if not pending else None,
                execution_price=Decimal("100") if not pending else None,
            )
        elif mutation.operation == "close":
            units, side, instrument = self.positions[mutation.target_id]
            closed = Decimal(mutation.payload.get("UnitsToDeduct", units))
            self.positions[mutation.target_id] = (units - closed, side, instrument)
            outcome = BrokerOutcome(
                IntentState.FILLED,
                mutation.request_id,
                order,
                mutation.target_id,
                closed,
                Decimal("0.5"),
                closed * (10 if side == "buy" else -10),
                execution_price=Decimal("110"),
            )
        else:
            outcome = BrokerOutcome(IntentState.CANCELED, mutation.request_id, mutation.target_id)
            prior = self.outcomes.get(mutation.target_id)
            if prior is not None:
                self.outcomes[mutation.target_id] = replace(prior, state=IntentState.CANCELED)
        self.outcomes[order] = outcome
        return outcome

    def lookup_order(self, context, order_id):
        self.lookups.append(order_id)
        return self.outcomes.get(order_id)

    lookup_close_order = lookup_order

    def quote(self, context, instrument_id, at):
        self.queries.append(instrument_id)
        if instrument_id in self.price_errors:
            raise RuntimeError("simulated quote failure")
        bid, ask = self.prices.get(instrument_id, (Decimal("110"), Decimal("112")))
        return PriceQuote(instrument_id, bid, ask, at)

    def collect_portfolio(self, context, observed_at):
        raise AssertionError("account totals must never be collected")
