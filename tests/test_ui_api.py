from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import dbzero as db0
import httpx
import pytest

from trader_api.broker.observations import PortfolioObservation, normalize_portfolio
from trader_api.broker.transport import HttpTransport
from trader_api.config import load_profile
from trader_api.domain import BrokerOutcome, IntentState
from trader_api.errors import TraderError
from trader_api.storage import (
    AccountingObservation,
    AuditEvent,
    ChartBucket,
    ControlEvent,
    ExecutionState,
    Intent,
    Operation,
    Order,
    Position,
    Preview,
    RefreshState,
    Reservation,
    ValuationSnapshot,
    close_dbzero,
)
from trader_api.ui_api import (
    Period,
    Resolution,
    get_dashboard,
    get_operation,
    get_portfolio_chart,
    get_refresh_status,
    get_statistics,
    list_accounting_observations,
    list_audit_events,
    list_operations,
    list_orders,
    list_positions,
    open_session,
    request_refresh,
    start_refresh,
    stop_refresh,
)
from trader_api.ui_api.session import selected
from trader_api.ui_api.updates import record_valuation

from .test_service_storage import _service


def setup_ui(runtime):
    service = _service(runtime)
    now = datetime.now(UTC).replace(microsecond=0)
    clock = SimpleNamespace(now=now)
    service._clock = lambda: clock.now
    record = service.store.scope_evidence(service._context.credential_fingerprint)
    record.expires_at = None
    service.store.commit(service.store.control_prefix)
    service.store.open(service.prefix)
    session = open_session(service, clock=lambda: clock.now)
    return service, session, clock


def collect(session, clock, equity=Decimal("100"), **kwargs):
    observation = PortfolioObservation(clock.now, equity, **kwargs)
    with selected(session, write=True):
        record_valuation(observation)
        RefreshState().last_success = clock.now


def tick(session):
    worker = session._worker
    worker.running = True
    return worker.tick()


def test_native_dashboard_statistics_restart_and_detachment(runtime):
    service, session, clock = setup_ui(runtime)
    service.initialize("2000")
    preview = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    service.submit(preview["preview_id"], "ui-open")
    operation = list_operations(session).items[0]
    assert operation.state == ExecutionState.FILLED
    assert isinstance(operation.intent, Intent)
    assert isinstance(operation.created_at, datetime)
    assert operation.params["strategy_notional_usd"] == Decimal("100.00")
    operation.params["strategy_notional_usd"] = Decimal("999")
    assert get_operation(session, operation.intent).operation.params["strategy_notional_usd"] == Decimal("100.00")
    detail = get_operation(session, operation.intent)
    assert detail.orders.items[0].intent == operation.intent
    assert detail.positions.items[0].position_id == "601"
    assert detail.accounting.items
    stats = get_statistics(session, Period.ALL)
    assert stats.confirmed_costs == Decimal("1.50")
    assert stats.confirmed_realized_pnl is None  # Opening notional is not profit.
    assert stats.operation_counts == {"COMMITTED": 0, "ADMITTED": 0, "FILLED": 1}
    collect(session, clock, Decimal("10000"), cash=Decimal("9900"), reasons=("missing_exposure",))
    dashboard = get_dashboard(session)
    assert dashboard.owner_allocation == Decimal("2000")
    assert dashboard.owner_budget.initial_cap == Decimal("2000")
    assert dashboard.valuation.equity == Decimal("10000")
    dashboard.operation_counts.clear()
    assert get_dashboard(session).operation_counts["FILLED"] == 1
    close_dbzero()
    restarted = _service(runtime)
    session2 = open_session(restarted, clock=lambda: clock.now)
    with pytest.raises(TraderError, match="no longer open"):
        get_dashboard(session)
    assert get_dashboard(session2).valuation.equity == Decimal("10000")
    assert get_statistics(session2, Period.ALL).confirmed_costs == Decimal("1.50")
    assert list_positions(session2).items[0].intent == list_operations(session2).items[0].intent
    assert restarted.verify_audit()["valid"] is True


def test_duplicate_outcomes_partial_close_and_late_confirmations(runtime):
    service, session, _clock = setup_ui(runtime)
    service.initialize("2000")
    preview = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    service.submit(preview["preview_id"], "open")
    intent = list_operations(session).items[0].intent
    reservation = service.store.one(Reservation, db0.as_tag(intent), prefix=service.prefix)
    state, binding = service._objects()
    outcome = BrokerOutcome(IntentState.FILLED, intent.request_id, "501", "601", actual_cost_usd=Decimal("1.50"))
    initial = state.strategy_realized
    generation = get_refresh_status(session).generation
    for _ in range(3):
        service._project_outcome(intent, reservation, outcome, dict(intent.params), state, binding)
    assert get_refresh_status(session).generation == generation
    assert state.strategy_realized == initial
    assert len(list_positions(session).items) == 1
    close = service.preview_close(position_id="601", fraction="0.5")
    service.submit(close["preview_id"], "half")
    closed = list_operations(session, operation=Operation.close).items[0].intent
    assert get_statistics(session, Period.ALL).confirmed_realized_pnl == Decimal("5")
    assert get_operation(session, closed).positions.items[0].position_id == "601"
    assert get_operation(session, closed).orders.items[0].order_id == "502"
    assert "incomplete_confirmed_costs" in get_statistics(session, Period.ALL).reasons
    close_reservation = service.store.one(Reservation, db0.as_tag(closed), prefix=service.prefix)
    outcome = BrokerOutcome(IntentState.FILLED, closed.request_id, "502", realized_pnl_usd=Decimal("5"))
    for _ in range(3):
        service._project_outcome(closed, close_reservation, outcome, dict(closed.params), state, binding)
    assert list_positions(session).items[0].strategy_notional_usd == Decimal("50")
    assert get_statistics(session, Period.ALL).confirmed_realized_pnl == Decimal("5")
    # A corrected cumulative amount contributes only its delta, including a confirmed zero cost.
    outcome = BrokerOutcome(IntentState.FILLED, closed.request_id, "502",
                            actual_cost_usd=Decimal("0"), realized_pnl_usd=Decimal("6"))
    service._project_outcome(closed, close_reservation, outcome, dict(closed.params), state, binding)
    assert get_statistics(session, Period.ALL).confirmed_realized_pnl == Decimal("6")
    assert list_positions(session).items[0].strategy_notional_usd == Decimal("50")
    costs = list_accounting_observations(session, intent=closed, kind="costs").items
    assert costs[0].confirmed and costs[0].amount == Decimal("0")
    assert "incomplete_confirmed_costs" not in get_statistics(session, Period.ALL).reasons
    first = list_accounting_observations(session, intent=closed, limit=1)
    assert first.next_cursor is not None
    assert list_accounting_observations(session, intent=closed, limit=1, cursor=first.next_cursor).items


def test_outcome_checkpoint_survives_restart_and_prevents_state_regression(runtime):
    service, session, _clock = setup_ui(runtime)
    service.initialize("2000")
    preview = service.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    service.submit(preview["preview_id"], "durable")
    close_dbzero()
    service = _service(runtime)
    session = open_session(service)
    intent = list_operations(session).items[0].intent
    reservation = service.store.one(Reservation, db0.as_tag(intent), prefix=service.prefix)
    state, binding = service._objects()
    generation = get_refresh_status(session).generation
    for reported_state in (IntentState.ACKNOWLEDGED, IntentState.FILLED, IntentState.FILLED):
        outcome = BrokerOutcome(reported_state, intent.request_id, "501", "601", actual_cost_usd=Decimal("1.50"))
        service._project_outcome(intent, reservation, outcome, dict(intent.params), state, binding)
    assert intent.state == ExecutionState.FILLED
    assert get_refresh_status(session).generation == generation
    assert get_statistics(session, Period.ALL).confirmed_costs == Decimal("1.50")
    assert len(list_positions(session).items) == 1


def test_period_boundaries_and_confirmed_delta_recognition(runtime):
    from trader_api.ui_api.updates import period_start, record_operation

    service, session, clock = setup_ui(runtime)
    monday = datetime(2026, 11, 2, tzinfo=UTC)
    assert period_start(Period.WEEK, monday + timedelta(days=6, hours=23), clock.now) == monday
    with service.store.transaction(service.prefix):
        preview = Preview(Operation.close, {}, clock.now, clock.now, "s", 1, 1)
        intent = Intent(preview, "x", Operation.close, {}, "x", "x", 1, 1, state=ExecutionState.FILLED)
        service.store.tag(intent, "INTENT")
        record_operation(intent, monday - timedelta(seconds=1),
                         BrokerOutcome(IntentState.FILLED, "x", realized_pnl_usd=Decimal("5")))
        record_operation(intent, monday, BrokerOutcome(IntentState.FILLED, "x", realized_pnl_usd=Decimal("5")))
    clock.now = monday
    # Identical outcomes cannot move yesterday's P&L into a new period.
    assert get_statistics(session, Period.TODAY).confirmed_realized_pnl is None
    with service.store.transaction(service.prefix):
        record_operation(intent, monday, BrokerOutcome(IntentState.FILLED, "x", realized_pnl_usd=Decimal("7")))
    assert get_statistics(session, Period.TODAY).confirmed_realized_pnl == Decimal("2")
    assert get_statistics(session, Period.WEEK).confirmed_realized_pnl == Decimal("2")
    assert get_statistics(session, Period.ALL).confirmed_realized_pnl == Decimal("7")


def test_indexed_pages_ties_appends_filters_and_scope(runtime):
    service, session, clock = setup_ui(runtime)
    with service.store.transaction(service.prefix):
        preview = Preview(Operation.open, {}, clock.now, clock.now + timedelta(minutes=5), "s", 1, 1)
        intents = [Intent(preview, str(i), Operation.open, {}, str(i), str(i), 1, 1, created_at=clock.now)
                   for i in range(8)]
        for intent in intents:
            order = Order(str(intent.sequence), intent, "AAPL", ExecutionState.PENDING)
            service.store.tag(order, "ORDER")
    first = list_operations(session, limit=3)
    assert [v.intent for v in first.items] == intents[:3]
    with service.store.transaction(service.prefix):
        later = Intent(preview, "later", Operation.open, {}, "later", "later", 1, 1, created_at=clock.now)
        service.store.tag(later, "INTENT")
    second = list_operations(session, limit=3, cursor=first.next_cursor)
    third = list_operations(session, limit=3, cursor=second.next_cursor)
    assert [v.intent for v in (*first.items, *second.items, *third.items)] == intents
    assert third.next_cursor is None
    with service.store.transaction(service.prefix):
        intents[0].state = ExecutionState.FILLED
    assert [v.intent for v in list_operations(session, state=ExecutionState.FILLED).items] == intents[:1]
    assert len(list_operations(session, start=clock.now, end=clock.now + timedelta(seconds=1)).items) == 9
    assert not list_operations(session, end=clock.now).items
    with pytest.raises(TraderError, match="different session or query"):
        list_operations(session, state=ExecutionState.FILLED, cursor=first.next_cursor)
    session2 = open_session(service, clock=lambda: clock.now)
    with pytest.raises(TraderError, match="different session or query"):
        list_operations(session2, cursor=first.next_cursor)
    orders = list_orders(session, symbol="AAPL", limit=2)
    assert len(list_orders(session, symbol="AAPL", limit=2, cursor=orders.next_cursor).items) == 2
    # Ambient prefix changes cannot redirect a captured session.
    service.store.open(service.store.control_prefix)
    assert len(list_operations(session).items) == 9
    foreign_prefix = service.store.trader_prefix("00000000-0000-0000-0000-000000000001")
    foreign_preview = Preview(Operation.open, {}, clock.now, clock.now, "s", 1, 1)
    foreign = Intent(foreign_preview, "x", Operation.open, {}, "x", "x", 1, 1)
    service.store.commit(foreign_prefix)
    with pytest.raises(TraderError, match="different trader"):
        get_operation(session, foreign)
    with pytest.raises(TraderError, match="different trader"):
        list_orders(session, intent=foreign)


def test_audit_inherited_indexes_tags_immutability_and_hash(runtime):
    service, session, _clock = setup_ui(runtime)
    state, _ = service._objects()
    with service.store.transaction(service.prefix):
        event = service.store.append_audit(service.prefix, state, kind="SAME", actor="actor", source="source",
                                           facts={"value": Decimal("1.25")})
        service.store.append_audit(service.prefix, state, kind="other", actor="SAME", source="source")
    assert len(list_audit_events(session, kind="SAME").items) == 1
    assert len(list_audit_events(session, actor="SAME").items) == 1
    assert len(list_audit_events(session, source="SAME").items) == 0
    page = list_audit_events(session, kind="SAME", start=event.occurred_at,
                             end=event.occurred_at + timedelta(milliseconds=1))
    assert page.items[0].event_hash == event.event_hash
    page.items[0].facts["value"] = Decimal("9")
    assert event.facts["value"] == Decimal("1.25")
    for field in ("kind", "actor", "source", "sequence", "occurred_at"):
        with pytest.raises(AttributeError, match="immutable"):
            setattr(event, field, getattr(event, field))
    for model, prefix in ((AuditEvent, service.prefix), (ControlEvent, service.store.control_prefix)):
        for field in ("sequence", "occurred_at"):
            index = db0.index_of(model, field, prefix=prefix)
            assert list(index.sort(db0.find(model, prefix=prefix)))
    assert service.verify_audit()["valid"]
    assert service.store.verify_control_audit()["valid"]


def test_incremental_chart_gaps_period_rollover_drawdown_and_atomicity(runtime, monkeypatch):
    service, session, clock = setup_ui(runtime)
    clock.now = datetime(2026, 12, 31, 23, 58, tzinfo=UTC)
    begin = clock.now
    collect(session, clock, Decimal("100"), cash=Decimal("20"))
    collect(session, clock, Decimal("100"))  # Same observation checkpoint, not a second sample.
    clock.now += timedelta(minutes=1)
    collect(session, clock, Decimal("120"))
    clock.now += timedelta(minutes=4)
    # Reads across midnight must not roll or rebuild persistent summaries.
    assert get_statistics(session, Period.TODAY).reasons == ("period_not_collected",)
    collect(session, clock, Decimal("90"), reasons=("missing_cash",))
    stats = get_statistics(session, Period.ALL)
    assert stats.absolute_equity_change == Decimal("-10")
    assert stats.sampled_equity_peak == Decimal("120")
    assert stats.maximum_drawdown == Decimal("30")
    assert stats.sample_count == 3 and stats.missing_intervals >= 3
    assert "missing_valuation_intervals" in stats.reasons
    assert get_statistics(session, Period.TODAY).sample_count == 1
    assert get_statistics(session, Period.MONTH).start == datetime(2027, 1, 1, tzinfo=UTC)
    chart = get_portfolio_chart(session, begin, clock.now + timedelta(minutes=1))
    assert chart.resolution == Resolution.MINUTE and len(chart.buckets) == 3
    assert chart.buckets[0].sample_count == 1
    assert get_portfolio_chart(session, begin, begin + timedelta(days=2)).resolution == Resolution.HOUR
    assert get_portfolio_chart(session, begin, begin + timedelta(days=100)).resolution == Resolution.DAY
    with pytest.raises(TraderError, match="1000"):
        get_portfolio_chart(session, begin, begin + timedelta(days=1001))
    generation = get_refresh_status(session).generation
    snapshots = len(db0.find(ValuationSnapshot, prefix=service.prefix))
    import trader_api.ui_api.updates as updates

    def fail(*args):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(updates, "aggregate", fail)
    clock.now += timedelta(minutes=1)
    with pytest.raises(RuntimeError):
        collect(session, clock, Decimal("80"))
    assert get_refresh_status(session).generation == generation
    assert len(db0.find(ValuationSnapshot, prefix=service.prefix)) == snapshots


def test_broker_normalization_unknowns_and_identity(runtime):
    service, _, clock = setup_ui(runtime)
    context = service._context
    result = normalize_portfolio({"clientPortfolio": {"credit": "10000"}}, context, clock.now)
    assert result.cash == Decimal("10000") and result.equity is None
    assert "missing_equity" in result.reasons
    for payload in ({"clientPortfolio": []}, {"clientPortfolio": {"budget": "2000"}},
                    {"clientPortfolio": {"equity": "nan"}}, {"clientPortfolio": {"equity": True}},
                    {"accountId": "wrong", "portfolioId": "portfolio-alpha", "equity": "10"}):
        with pytest.raises(TraderError):
            normalize_portfolio(payload, context, clock.now)


def test_etoro_observation_is_single_read_and_honors_retry_after(runtime):
    from trader_api.broker.etoro import EtoroBrokerAdapter

    service, _session, clock = setup_ui(runtime)
    profile = load_profile(runtime["profile"])
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"clientPortfolio": {"equity": "900", "credit": "100"}})

    adapter = EtoroBrokerAdapter(profile, HttpTransport(profile, httpx.Client(transport=httpx.MockTransport(respond))))
    observation = adapter.collect_portfolio(service._context, clock.now)
    assert observation.equity == Decimal("900") and observation.cash == Decimal("100")
    assert observation.unrealized_pnl is None
    assert len(calls) == 1 and calls[0].method == "GET"
    transport = HttpTransport(profile, httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(429, headers={"Retry-After": "420"})
    )))
    with pytest.raises(TraderError) as error:
        transport.request(profile.routes["ETORO_PNL_URL"])
    assert error.value.details == {"retry_after_seconds": 420}


def test_failure_backoff_and_interruption_survive_restart(runtime):
    service, session, clock = setup_ui(runtime)

    def failure(context, at):
        raise RuntimeError("secret broker response")

    service.broker.collect_portfolio = failure
    for delay in (60, 120, 240, 300, 300):
        request_refresh(session)
        assert tick(session)
        status = get_refresh_status(session)
        assert status.error == "REFRESH_FAILED" and status.last_success is None
        assert status.next_attempt == clock.now + timedelta(seconds=delay)
        clock.now += timedelta(seconds=delay)
    with selected(session, write=True):
        RefreshState().in_progress = True
    close_dbzero()
    service = _service(runtime)
    session = open_session(service, clock=lambda: clock.now - timedelta(seconds=1))
    assert get_refresh_status(session).error == "REFRESH_INTERRUPTED"
    start_refresh(session)
    try:
        status = get_refresh_status(session)
        assert status.next_attempt == clock.now
        assert status.last_success is None
    finally:
        stop_refresh(session)


def test_fake_clock_refresh_coalescing_backoff_staleness_and_last_good(runtime):
    service, session, clock = setup_ui(runtime)
    calls = []

    def reader(context, at):
        calls.append(at)
        for _ in range(4):
            request_refresh(session)
        return PortfolioObservation(at, Decimal("123"))

    service.broker.collect_portfolio = reader
    request_refresh(session)
    assert not get_refresh_status(session).running
    assert tick(session)
    assert len(calls) == 1 and not get_refresh_status(session).queued
    assert not tick(session)
    clock.now += timedelta(seconds=60)
    assert tick(session)
    assert len(calls) == 2
    original = get_dashboard(session).valuation

    def failure(context, at):
        raise TraderError("BROKER_RATE_LIMITED", "secret payload", retryable=True,
                          details={"retry_after_seconds": 400})

    service.broker.collect_portfolio = failure
    request_refresh(session)
    assert tick(session)
    status = get_refresh_status(session)
    assert status.error == "BROKER_RATE_LIMITED"
    assert status.next_attempt == clock.now + timedelta(seconds=400)
    request_refresh(session)
    clock.now += timedelta(seconds=121)
    assert not tick(session)
    assert get_refresh_status(session).stale
    assert get_dashboard(session).valuation == original
    clock.now += timedelta(seconds=279)
    assert tick(session)
    stop_refresh(session)
    stop_refresh(session)
    assert not get_refresh_status(session).running


def test_real_thread_start_stop_no_overlap_and_scope_revocation(runtime):
    service, session, clock = setup_ui(runtime)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def reader(context, at):
        calls.append(at)
        entered.set()
        assert release.wait(5)
        return PortfolioObservation(at, Decimal("100"))

    service.broker.collect_portfolio = reader
    start_refresh(session)
    start_refresh(session)
    assert entered.wait(5)
    for _ in range(10):
        request_refresh(session)
    assert get_refresh_status(session).refreshing
    assert len(calls) == 1
    release.set()
    stop_refresh(session)
    assert get_refresh_status(session).last_success == clock.now
    record = service.store.scope_evidence(service._context.credential_fingerprint)
    record.revoked = True
    service.store.commit(service.store.control_prefix)
    request_refresh(session)
    assert tick(session)
    assert get_refresh_status(session).error == "PERMISSION_REVOKED"
    assert len(calls) == 1
    stop_refresh(session)


def test_bounded_large_history_and_no_history_on_summary_reads(runtime, monkeypatch):
    service, session, clock = setup_ui(runtime)
    with service.store.transaction(service.prefix):
        preview = Preview(Operation.open, {}, clock.now, clock.now, "s", 1, 1)
        for i in range(1500):
            intent = Intent(preview, str(i), Operation.open, {}, str(i), str(i), 1, 1, created_at=clock.now)
            service.store.tag(intent, "INTENT")
    original_index = db0.index_of
    yielded = []

    class CountedIndex:
        def __init__(self, index):
            self.index = index

        def select(self, *args, **kwargs):
            return self.index.select(*args, **kwargs)

        def sort(self, *args, **kwargs):
            for value in self.index.sort(*args, **kwargs):
                yielded.append(value)
                yield value

    monkeypatch.setattr(db0, "index_of", lambda *args, **kwargs: CountedIndex(original_index(*args, **kwargs)))
    assert len(list_operations(session, limit=100).items) == 100
    assert len(yielded) == 102  # One indexed ceiling, page size plus one lookahead.
    original_find = db0.find

    def guarded_find(model, *args, **kwargs):
        assert model not in (Intent, Position, Order, AuditEvent, AccountingObservation, ValuationSnapshot, ChartBucket)
        return original_find(model, *args, **kwargs)

    monkeypatch.setattr(db0, "find", guarded_find)
    assert get_dashboard(session).valuation.equity is None
    assert get_statistics(session, Period.ALL).sample_count == 0


def test_chart_materialization_is_bounded_and_does_not_read_snapshots(runtime, monkeypatch):
    from trader_api.ui_api.updates import aggregate_key

    service, session, clock = setup_ui(runtime)
    begin = clock.now.replace(second=0)
    with service.store.transaction(service.prefix):
        for offset in range(1200):
            at = begin + timedelta(minutes=offset)
            bucket = ChartBucket(aggregate_key(Resolution.MINUTE, at), "minute", at, sample_count=1)
            service.store.tag(bucket, "UI_BUCKET")
    original_find = db0.find

    def guarded_find(model, *args, **kwargs):
        assert model is not ValuationSnapshot
        return original_find(model, *args, **kwargs)

    monkeypatch.setattr(db0, "find", guarded_find)
    chart = get_portfolio_chart(session, begin, begin + timedelta(minutes=1000), resolution=Resolution.MINUTE)
    assert len(chart.buckets) == 1000
    assert chart.buckets[-1].start == begin + timedelta(minutes=999)
    with pytest.raises(TraderError, match="1000"):
        get_portfolio_chart(session, begin, begin + timedelta(minutes=1001), resolution=Resolution.MINUTE)


def test_errors_and_unavailable_collection(runtime):
    _service_value, session, clock = setup_ui(runtime)
    for size in (0, 1001, True):
        with pytest.raises(TraderError):
            list_operations(session, limit=size)
    with pytest.raises(TraderError):
        list_operations(session, start=datetime(2026, 1, 1))
    with pytest.raises(TraderError):
        get_portfolio_chart(session, clock.now, clock.now)
    assert tick(session)
    assert get_refresh_status(session).error == "VALUATION_UNAVAILABLE"
    assert get_dashboard(session).valuation.equity is None
    stop_refresh(session)
    close_dbzero()
    with pytest.raises(TraderError, match="no longer open"):
        get_dashboard(session)
