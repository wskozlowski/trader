"""Incremental write-side statistics. Call within the source transaction and DB lock."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import cast

import dbzero as db0  # type: ignore[import-untyped]

from ..broker.observations import PortfolioObservation
from ..domain import BrokerOutcome
from ..serialization import storage_datetime
from ..storage import (
    AccountingObservation,
    ChartBucket,
    CurrentSummary,
    EquityAggregate,
    ExecutionState,
    Intent,
    Operation,
    OperationContribution,
    PeriodSummary,
    ValuationSnapshot,
    next_sequence,
)
from .types import Period, Resolution

SECONDS = {Resolution.MINUTE: 60, Resolution.HOUR: 3600, Resolution.DAY: 86400}


def period_start(period: Period, now: datetime, started: datetime) -> datetime:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period is Period.TODAY:
        return day
    if period is Period.WEEK:
        return day - timedelta(days=day.weekday())
    if period is Period.MONTH:
        return day.replace(day=1)
    return started


def bucket_start(resolution: Resolution, at: datetime) -> datetime:
    if resolution is Resolution.MINUTE:
        return at.replace(second=0, microsecond=0)
    if resolution is Resolution.HOUR:
        return at.replace(minute=0, second=0, microsecond=0)
    return at.replace(hour=0, minute=0, second=0, microsecond=0)


def aggregate_key(kind: str, start: datetime) -> str:
    return f"{kind}:{start.isoformat()}"


def current(now: datetime) -> CurrentSummary:
    return CurrentSummary(storage_datetime(now))


def periods(summary: CurrentSummary, now: datetime) -> tuple[PeriodSummary, ...]:
    result = []
    for period in Period:
        start = period_start(period, now, summary.started_at)
        key = aggregate_key(period, start)
        item = next(iter(db0.find(PeriodSummary, key)), None)
        if item is None:
            reasons = ("collection_started_mid_period",) if start < summary.started_at else ()
            item = PeriodSummary(key, str(period), start, reasons=reasons,
                                 operation_counts=dict(summary.operation_counts),
                                 unconfirmed_costs=summary.unconfirmed_costs,
                                 unconfirmed_realized_pnl=summary.unconfirmed_realized_pnl)
            db0.tags(item).add("UI_PERIOD")
        result.append(item)
    return tuple(result)


def _reasons(existing: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*existing, *extra)))


def aggregate(item: EquityAggregate, observation: PortfolioObservation, generation: int) -> None:
    at = observation.observed_at
    if item.first_observed_at is None:
        item.first_observed_at = at
    if item.last_observed_at is not None:
        gap = (at - item.last_observed_at).total_seconds()
        if gap > 120:
            item.missing_intervals += max(1, int(gap // 60) - 1)
            item.reasons = _reasons(item.reasons, ("missing_valuation_intervals",))
    item.last_observed_at = at
    item.sample_count += 1
    item.generation = generation
    item.reasons = _reasons(item.reasons, observation.reasons)
    for name in ("cash", "unrealized_pnl", "exposure"):
        value = getattr(observation, name)
        if value is not None:
            setattr(item, name, value)
    equity = observation.equity
    if equity is not None:
        if item.first_equity is None:
            item.first_equity = equity
        item.last_equity = equity
        item.min_equity = equity if item.min_equity is None else min(item.min_equity, equity)
        item.max_equity = equity if item.max_equity is None else max(item.max_equity, equity)
        item.max_drawdown = max(item.max_drawdown or Decimal(0), item.max_equity - equity)


def record_valuation(observation: PortfolioObservation) -> None:
    summary = current(observation.observed_at)
    if summary.last_observed_at is not None and observation.observed_at <= summary.last_observed_at:
        # Same collection retried after commit; never count it twice or reorder peaks.
        return
    summary.generation += 1
    snapshot = ValuationSnapshot(
        next_sequence(), observation.observed_at, observation.equity, observation.cash,
        observation.unrealized_pnl, observation.exposure, observation.reasons, summary.generation,
    )
    db0.tags(snapshot).add("UI_SNAPSHOT")
    for item in periods(summary, observation.observed_at):
        if item.first_observed_at is None:
            gap = (observation.observed_at - max(item.start, summary.started_at)).total_seconds()
            if gap > 120:
                item.missing_intervals += int(gap // 60)
                item.reasons = _reasons(item.reasons, ("missing_valuation_intervals",))
        aggregate(item, observation, summary.generation)
    for resolution in Resolution:
        start = bucket_start(resolution, observation.observed_at)
        key = aggregate_key(resolution, start)
        bucket = next(iter(db0.find(ChartBucket, key)), None)
        if bucket is None:
            reasons = ("collection_started_mid_bucket",) if start < summary.started_at else ()
            bucket = ChartBucket(key, str(resolution), start, reasons=reasons)
            db0.tags(bucket).add("UI_BUCKET")
        aggregate(bucket, observation, summary.generation)
        coverage_end = start + timedelta(seconds=SECONDS[resolution] - 60)
        assert bucket.first_observed_at is not None and bucket.last_observed_at is not None
        complete_span = (
            bucket.first_observed_at <= start + timedelta(seconds=60)
            and bucket.last_observed_at >= coverage_end
        )
        coverage_reasons = tuple(reason for reason in bucket.reasons if reason != "partial_bucket")
        bucket.reasons = coverage_reasons if complete_span else _reasons(coverage_reasons, ("partial_bucket",))
    summary.last_observed_at = observation.observed_at
    summary.equity = observation.equity
    summary.cash = observation.cash
    summary.unrealized_pnl = observation.unrealized_pnl
    summary.exposure = observation.exposure
    summary.reasons = observation.reasons


def contribution(intent: Intent) -> OperationContribution:
    existing = next(iter(db0.find(OperationContribution, db0.as_tag(intent))), None)
    if existing is not None:
        return cast(OperationContribution, existing)
    item = OperationContribution(intent)
    db0.tags(item).add("UI_CONTRIBUTION")
    return item


def record_operation(intent: Intent, now: datetime, outcome: BrokerOutcome | None = None) -> None:
    """Apply changes relative to the persisted per-operation checkpoint, never ledger notional."""
    now = storage_datetime(now)
    summary = current(now)
    checkpoint = contribution(intent)
    state = str(intent.state)
    changed_state = checkpoint.state != state
    changes: list[tuple[str, Decimal | None, Decimal | None, bool]] = []
    estimated = intent.params.get("estimated_strategy_cost_usd")
    if estimated is not None and checkpoint.estimated_costs is None:
        estimate = Decimal(estimated)
        checkpoint.estimated_costs = estimate
        changes.append(("estimated_costs", estimate, None, False))
    if outcome is not None:
        for name, amount in (("costs", outcome.actual_cost_usd), ("realized_pnl", outcome.realized_pnl_usd)):
            previous = getattr(checkpoint, name)
            if amount is not None and amount != previous:
                changes.append((name, amount, amount - (previous or Decimal(0)), True))
                setattr(checkpoint, name, amount)
    for name, expected in (
        ("costs", intent.state == ExecutionState.FILLED and intent.operation in {Operation.open, Operation.close}),
        ("realized_pnl", intent.state == ExecutionState.FILLED and intent.operation == Operation.close),
    ):
        missing = expected and getattr(checkpoint, name) is None
        old_missing = getattr(checkpoint, f"{name}_missing")
        if missing != old_missing:
            setattr(checkpoint, f"{name}_missing", missing)
            counter = f"unconfirmed_{name}"
            setattr(summary, counter, getattr(summary, counter) + int(missing) - int(old_missing))
            changes.append((f"{name}_{'unavailable' if missing else 'confirmed'}", None, None, False))
    if not changed_state and not changes:
        return
    summary.generation += 1
    summaries = periods(summary, now)
    if changed_state:
        counts = dict(summary.operation_counts)
        if checkpoint.state is not None:
            counts[checkpoint.state] = counts.get(checkpoint.state, 0) - 1
        counts[state] = counts.get(state, 0) + 1
        summary.operation_counts = counts
        checkpoint.state = state
        changes.append((f"state:{state}", None, None, False))
    for kind, amount, delta, confirmed in changes:
        sequence = next_sequence()
        observation = AccountingObservation(sequence, now, intent, kind, amount, delta, confirmed, summary.generation)
        db0.tags(observation).add("UI_ACCOUNTING")
        if confirmed:
            assert delta is not None
            setattr(summary, kind, (getattr(summary, kind) or Decimal(0)) + delta)
            for period in summaries:
                setattr(period, kind, (getattr(period, kind) or Decimal(0)) + delta)
        summary.accounting_checkpoint = sequence
    for period in summaries:
        period.operation_counts = dict(summary.operation_counts)
        period.unconfirmed_costs = summary.unconfirmed_costs
        period.unconfirmed_realized_pnl = summary.unconfirmed_realized_pnl
        period.generation = summary.generation
