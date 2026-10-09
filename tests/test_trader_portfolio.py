from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from threading import Event
from types import SimpleNamespace

import pytest

from trader_api.domain import IntentState, utc_now
from trader_api.errors import TraderError
from trader_api.service import TraderService
from trader_api.storage import ExecutionState, close_dbzero
from trader_api.ui_api import get_dashboard, list_operations, list_orders, list_positions, open_session
from trader_api.ui_api.commands import CancelOrderRequest, ClosePositionRequest, prepare_cancel, prepare_close

from .simulated_broker import SimulatedBroker
from .test_standalone import direct_service


def setup(runtime):
    broker = SimulatedBroker()
    service = direct_service(runtime, broker)
    clock = SimpleNamespace(now=utc_now().replace(microsecond=0))
    service._clock = lambda: clock.now
    session = open_session(service, clock=lambda: clock.now)
    service.initialize("1000")
    return service, session, broker, clock


def opening(service, **kwargs):
    preview = service.preview_open(symbol="ETH", side=kwargs.pop("side", "long"), strategy_notional_usd="100", **kwargs)
    return service.submit(preview["preview_id"], preview["preview_id"])


def refresh(session):
    session._worker._collect(session._clock())


@pytest.mark.parametrize("side,pnl", [("long", "10"), ("short", "-12")])
def test_local_valuation_cached_restart_and_freshness(runtime, side, pnl):
    service, session, broker, clock = setup(runtime)
    assert get_dashboard(session).valuation.total_pnl == 0
    assert list_positions(session).items == ()
    opening(service, side=side, leverage=2, stop_loss_rate="80")
    assert get_dashboard(session).valuation.unrealized_pnl is None
    refresh(session)
    value = get_dashboard(session).valuation
    assert value.unrealized_pnl == Decimal(pnl)  # No second leverage multiplication.
    assert value.realized_pnl == -1
    assert value.total_pnl == Decimal(pnl) - 1
    assert broker.queries == [1001]  # External position 9999 is never requested.
    position = list_positions(session).items[0]
    assert position.entry_price == 100 and position.remaining_units == 1
    clock.now += timedelta(seconds=90)
    assert not get_dashboard(session).valuation.price_stale
    assert not list_positions(session).items[0].price_stale
    clock.now += timedelta(microseconds=1)
    assert get_dashboard(session).valuation.price_stale
    assert list_positions(session).items[0].price_stale
    broker.price_errors.add(1001)
    refresh(session)
    assert get_dashboard(session).valuation.total_pnl == value.total_pnl
    assert get_dashboard(session).valuation.observed_at == value.observed_at
    close_dbzero()
    restarted = direct_service(runtime, broker)
    other = open_session(restarted, clock=lambda: clock.now)
    assert get_dashboard(other).valuation.total_pnl == value.total_pnl
    assert get_dashboard(other).valuation.observed_at == value.observed_at


def test_partial_full_close_late_confirmation_and_duplicate(runtime):
    service, session, broker, _clock = setup(runtime)
    opened = opening(service)
    refresh(session)
    preview = service.preview_close(position_id=opened["broker_position_id"], fraction="0.5")
    service.submit(preview["preview_id"], "half")
    service.submit(preview["preview_id"], "half")
    assert len(broker.mutations) == 2
    assert list_positions(session).items[0].remaining_units == Decimal("0.5")
    value = get_dashboard(session).valuation
    assert value.unrealized_pnl == 5 and value.realized_pnl == Decimal("3.5")
    # Corrected costs arrive late via the existing locally owned order lookup.
    last = list_operations(session).items[-1].intent
    broker.outcomes[last.broker_order_id] = replace(broker.outcomes[last.broker_order_id], actual_cost_usd=Decimal("1"))
    import dbzero as db0

    from trader_api.storage import Reservation

    reservation = service.store.one(Reservation, db0.as_tag(last), prefix=service.prefix)
    state, binding = service._objects()
    for _ in range(2):
        service._project_outcome(
            last, reservation, broker.outcomes[last.broker_order_id], dict(last.params), state, binding
        )
    service.reconcile()
    assert get_dashboard(session).valuation.realized_pnl == 3
    assert list_positions(session).items[0].remaining_units == Decimal("0.5")
    preview = service.preview_close(position_id=opened["broker_position_id"])
    service.submit(preview["preview_id"], "rest")
    assert get_dashboard(session).valuation.unrealized_pnl == 0
    assert get_dashboard(session).valuation.realized_pnl == Decimal("7.5")


def test_same_broker_account_scopes_ownership_budget_and_history(runtime):
    alpha, a, broker, clock = setup(runtime)
    beta = TraderService("beta", runtime["profile"], broker=broker, storage_root=runtime["root"])
    b = open_session(beta, clock=lambda: clock.now)
    beta.initialize("500")
    opened = opening(alpha)
    opening(beta, side="short")
    pending = opening(alpha, order_type="market_if_touched", trigger_rate="90")
    refresh(a)
    refresh(b)
    assert len(list_positions(a).items) == len(list_positions(b).items) == 1
    assert len(list_operations(a).items) == 2 and len(list_operations(b).items) == 1
    assert get_dashboard(a).strategy_budget.available_to_open == 797
    assert get_dashboard(b).strategy_budget.available_to_open == 399
    assert get_dashboard(a).valuation.unrealized_pnl == 10
    assert get_dashboard(b).valuation.unrealized_pnl == -12
    with pytest.raises(TraderError):
        prepare_close(b, ClosePositionRequest(list_positions(a).items[0].position, Decimal(1)))
    with pytest.raises(TraderError):
        prepare_cancel(b, CancelOrderRequest(list_orders(a, state=ExecutionState.PENDING).items[0].order))
    with pytest.raises(TraderError):
        beta.preview_close(position_id=opened["broker_position_id"])
    with pytest.raises(TraderError):
        beta.preview_cancel(order_id=pending["broker_order_id"])
    assert "external" in broker.positions


def test_missing_confirmed_fill_and_cost_never_uses_preview(runtime):
    service, session, broker, _clock = setup(runtime)
    dispatch = broker.dispatch

    def incomplete(context, mutation):
        return replace(dispatch(context, mutation), filled_units=None, execution_price=None, actual_cost_usd=None)

    broker.dispatch = incomplete
    opening(service)
    value = get_dashboard(session).valuation
    assert value.realized_pnl is value.unrealized_pnl is value.total_pnl is None
    refresh(session)  # Confirmed lookup backfills the local record.
    assert get_dashboard(session).valuation.total_pnl == 9
    assert list_positions(session).items[0].remaining_units == 1
    assert broker.lookups == ["1001"]


def test_per_position_prices_oldest_aggregate_timestamp(runtime):
    service, session, broker, clock = setup(runtime)
    opening(service)
    preview = service.preview_open(instrument_id=2002, side="long", strategy_notional_usd="100")
    service.submit(preview["preview_id"], "second")
    refresh(session)
    oldest = clock.now
    broker.price_errors.add(1001)
    clock.now += timedelta(seconds=91)
    refresh(session)
    positions = list_positions(session).items
    assert positions[0].price_stale and not positions[1].price_stale
    assert get_dashboard(session).valuation.observed_at == oldest
    assert get_dashboard(session).valuation.unrealized_pnl == 20


def test_broker_io_releases_database_and_duplicate_submissions_serialize(runtime):
    service, session, broker, _clock = setup(runtime)
    preview = service.preview_open(symbol="ETH", side="long", strategy_notional_usd="100")
    entered, release = Event(), Event()
    dispatch = broker.dispatch

    def blocked(context, mutation):
        entered.set()
        assert release.wait(5)
        return dispatch(context, mutation)

    broker.dispatch = blocked
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(service.submit, preview["preview_id"], "once")
        assert entered.wait(3)
        second = pool.submit(service.submit, preview["preview_id"], "once")
        dashboard = pool.submit(get_dashboard, session).result(timeout=2)
        assert dashboard.strategy_budget.committed == 102
        release.set()
        assert first.result(timeout=3) == second.result(timeout=3)
    assert len(broker.mutations) == 1


def test_unknown_close_cannot_be_replaced_and_stale_size_rejected(runtime):
    service, _session, broker, _clock = setup(runtime)
    opened = opening(service)
    preview = service.preview_close(position_id=opened["broker_position_id"], fraction="0.5")
    other = service.preview_close(position_id=opened["broker_position_id"], fraction="0.5")
    dispatch = broker.dispatch

    def unknown(context, mutation):
        return replace(dispatch(context, mutation), state=IntentState.UNKNOWN)

    broker.dispatch = unknown
    service.submit(preview["preview_id"], "unknown")
    with pytest.raises(TraderError, match="existing close"):
        service.submit(other["preview_id"], "replacement")
    broker.lookup_request = lambda context, request: next(
        o for o in broker.outcomes.values() if o.request_id == request
    )
    service.reconcile()
    with pytest.raises(TraderError, match="size changed"):
        service.submit(other["preview_id"], "after-reconcile")


def test_legacy_migration_keeps_confirmed_costs_without_deducting_twice(runtime):
    import dbzero as db0

    from trader_api.domain import BrokerOutcome
    from trader_api.storage import (
        Intent,
        LocalAccountingVersion,
        Operation,
        OperationContribution,
        Position,
        Preview,
        Reservation,
        Side,
    )

    service, session, broker, clock = setup(runtime)
    with service.store.transaction(service.prefix):
        state, _binding = service._objects()
        params = {
            "symbol": "ETH",
            "instrument_id": 1001,
            "side": "long",
            "leverage": 1,
            "strategy_notional_usd": Decimal(100),
            "estimated_strategy_cost_usd": Decimal(2),
        }
        preview = Preview(Operation.open, params, clock.now, clock.now + timedelta(minutes=5), "state-1", 0, 1)
        intent = Intent(preview, "legacy", Operation.open, params, "old-request", "digest", 0, 1)
        intent.state = intent.projected_state = ExecutionState.FILLED
        intent.broker_order_id, intent.broker_position_id = "old-order", "old-position"
        db0.tags(intent).add("INTENT")
        reservation = Reservation(intent, Decimal(102), Decimal(0), 0, 1)
        db0.tags(reservation).add("RESERVATION")
        position = Position("old-position", intent, "ETH", Side.long, 1001, 1, Decimal(100), Decimal(1))
        db0.tags(position).add("POSITION")
        checkpoint = OperationContribution(intent, state="FILLED", costs=Decimal("1.5"))
        db0.tags(checkpoint).add("UI_CONTRIBUTION")
        state.strategy_realized, state.strategy_committed = Decimal("-1.5"), Decimal(100)
        LocalAccountingVersion().initialized = False
    broker.outcomes["old-order"] = BrokerOutcome(
        IntentState.FILLED,
        "old-request",
        "old-order",
        "old-position",
        Decimal(1),
        Decimal("1.5"),
        execution_price=Decimal(100),
    )
    service.refresh_execution_details()
    refresh(session)
    assert get_dashboard(session).strategy_budget.settled_realized_pnl == Decimal("-1.5")
    assert get_dashboard(session).strategy_budget.committed == 100
    assert get_dashboard(session).valuation.total_pnl == Decimal("8.5")


def test_late_close_units_release_only_confirmed_size(runtime):
    service, session, broker, _clock = setup(runtime)
    opened = opening(service)
    preview = service.preview_close(position_id=opened["broker_position_id"], fraction="0.5")
    dispatch = broker.dispatch
    broker.dispatch = lambda context, mutation: replace(dispatch(context, mutation), filled_units=None)
    service.submit(preview["preview_id"], "late-units")
    assert list_positions(session).items[0].remaining_units is None
    assert get_dashboard(session).strategy_budget.committed == 100
    service.refresh_execution_details()
    assert list_positions(session).items[0].remaining_units == Decimal("0.5")
    assert get_dashboard(session).strategy_budget.committed == 50


def test_native_model_layout_survives_cyclic_collection(runtime):
    import gc

    from trader_api.storage import Position, Trader

    service, session, _broker, _clock = setup(runtime)
    opening(service)
    refresh(session)
    for model in (Trader, Position):
        assert model.__dictoffset__ == 0
        assert model.__weakrefoffset__ == 0
    for _ in range(3):
        gc.collect()
        assert get_dashboard(session).valuation.total_pnl == 9
        assert list_positions(session).items[0].remaining_units == 1


def test_policy_change_during_broker_read_is_revalidated(runtime):
    service, _session, broker, _clock = setup(runtime)
    preview = service.preview_open(symbol="ETH", side="long", strategy_notional_usd="100")
    entered, release = Event(), Event()

    def fingerprint(_context):
        entered.set()
        assert release.wait(5)
        return broker.fingerprint

    broker.state_fingerprint = fingerprint
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.submit, preview["preview_id"], "policy-race")
        assert entered.wait(3)
        with service.store.transaction(service.prefix):
            state, _binding = service._objects()
            state.policy_version += 1
        release.set()
        with pytest.raises(TraderError, match="binding or policy changed"):
            future.result(timeout=3)
    assert not broker.mutations


def test_two_traders_can_dispatch_concurrently_without_crossing_database_prefixes(runtime):
    from threading import Barrier

    alpha, a, broker, clock = setup(runtime)
    beta = TraderService("beta", runtime["profile"], broker=broker, storage_root=runtime["root"])
    b = open_session(beta, clock=lambda: clock.now)
    beta.initialize("500")
    first = alpha.preview_open(symbol="ETH", side="long", strategy_notional_usd="100")
    second = beta.preview_open(symbol="ETH", side="short", strategy_notional_usd="200")
    barrier = Barrier(2)
    dispatch = broker.dispatch

    def concurrent(context, mutation):
        barrier.wait(timeout=3)
        return dispatch(context, mutation)

    broker.dispatch = concurrent
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(alpha.submit, first["preview_id"], "alpha")
        two = pool.submit(beta.submit, second["preview_id"], "beta")
        assert one.result(timeout=5)["state"] == two.result(timeout=5)["state"] == "FILLED"
    assert list_positions(a).items[0].strategy_notional_usd == 100
    assert list_positions(b).items[0].strategy_notional_usd == 200
    assert get_dashboard(a).strategy_budget.available_to_open == 899
    assert get_dashboard(b).strategy_budget.available_to_open == 299


def test_confirmed_open_without_position_handle_waits_then_backfills_owned_order(runtime):
    service, session, broker, _clock = setup(runtime)
    dispatch = broker.dispatch
    broker.dispatch = lambda context, mutation: replace(dispatch(context, mutation), broker_position_id=None)
    opening(service)
    assert not list_positions(session).items
    assert get_dashboard(session).valuation.total_pnl is None
    refresh(session)
    assert len(list_positions(session).items) == 1
    assert get_dashboard(session).valuation.total_pnl == 9
    assert get_dashboard(session).strategy_budget.available_to_open == 899
