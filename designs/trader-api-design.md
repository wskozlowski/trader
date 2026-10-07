# Agentic eToro trading CLI and Python API

## Purpose and boundaries

`trader` is a Python service library with a machine-readable command-line interface for an agent that trades a **real-money eToro account**. Its first state-changing command establishes a fixed amount of initial capital. Subsequent trading can recycle returned principal and spend settled, realized net profits without a separate profits cap. Full leveraged notional, not merely margin, counts against available trading capital.

The supported workflow is opening long or short positions at market or by pending order, setting take-profit (TP) and stop-loss (SL), selecting leverage at open, modifying supported position/order settings, partially or fully closing positions, and canceling pending orders. Copy trading, deposits, withdrawals, transfers, and other account administration are **out of scope**. Every action the broker actually supports must be exposed consistently through the Python API and CLI. Availability depends on the account, instrument, jurisdiction, and authenticated official eToro API: there must be no screen scraping, private endpoints, fictitious broker responses, or automatic conversion of an unsupported action into a different trade.

This document is a design, not a claim that a particular eToro endpoint, demo environment, or `dbzero-pro` version has been verified. Before implementation, validate the approved integration's authentication, account binding, allowed order types, TP/SL units, modification and partial-close capabilities, idempotency/reconciliation identifiers, rate limits, and demo-account support. Publish discovered capabilities through `trader capabilities`; reject unsupported operations with `UNSUPPORTED_CAPABILITY` before accepting a preview. If an official development/demo endpoint cannot be verified, development trading fails closed; it must never fall back to the live account.

This budget limits **CLI-initiated opening notional**, not the account's worst-case loss. Gaps, leverage, fees, liquidation, manual account activity, and broker execution may cause losses beyond the configured initial amount. SL and TP are instructions to the broker, not guarantees of execution price. No trade occurs merely because an agent has requested a preview.

## Runtime, trust boundary, and storage

The executable and importable library share one `TraderService` and one validated broker adapter; the CLI is not a second trading implementation. Use `Decimal` for money, UTC timestamps, the eToro account's reported base currency as the budget currency, and explicit rounding up for estimated costs/reservations. Never use binary floating-point arithmetic for budget decisions. Credentials come from a secret store or process environment, are scoped to the selected account/environment, and never appear in command arguments, JSON output, memo objects, or audit payloads. Agent callers may invoke the constrained service API, not arbitrary dbzero mutations or the raw broker client. Production writes should run through a single authorized trader service; enforce serialization at the budget/command boundary even with multiple agents or processes.

| Environment | Required dbzero root | Broker identity |
| --- | --- | --- |
| `production` | `/dbzero-data/trader` | Explicitly configured and verified live eToro account |
| `development` | `/dbzero-data/trader-dev` | Explicitly configured and verified official demo/sandbox account |

Configure dbzero's **physical root** as the corresponding absolute path, and give its memo models an environment-specific **logical prefix** such as `/trader/production/data` or `/trader/development/data`; a logical prefix is not a substitute for the physical root. Every dbzero-managed file, index, journal, snapshot, and test fixture belongs inside the selected root (tests use isolated subdirectories under `/dbzero-data/trader-dev`). Resolve and validate paths, including symlinks, before opening the store; reject a root outside its allowlisted directory, an environment/account mismatch, or an attempt to change the initialized account. Do not rely on the working directory or a user-provided arbitrary `--db-path`. Provision the development directory before use if it does not exist. Keep separate broker credentials and never copy production memo data into development.

Only `--help`, `version`, and `init` may run before initialization. For example, `trader --env production init --balance 1000.00 --currency USD` authenticates the configured account, verifies its base currency and available cash of at least USD 1,000, records the broker account ID, currency, starting account snapshot, fixed initial capital, and policy version, then durably commits them. `init` is single-use per environment/account; retry with identical input is idempotent, while a different amount/account fails. No later `top-up` operation exists. An external deposit is not a budget credit. If the live account already contains positions, they remain outside the CLI allocation unless they were previously recorded as CLI-created and reconciled.

## Persistent object graph and audit trail

All durable application entities are dbzero memo objects: ordinary Python classes decorated with `@db0.memo(prefix=DATA_PREFIX, ...)`, using references to other memo objects and tags for scoped queries. Follow the conventions in `/src/selltime/AGENTS.md`: singleton root where appropriate; `db0.tags(obj).add(...)`, `db0.find(Class, ...)`, and indexed queries instead of Python-side scanning. The following is the logical schema, not a promise of unverified decorator or broker field signatures:

| Memo type | Durable content and relationships | Useful tags/indexes |
| --- | --- | --- |
| `TraderAccount` singleton and `BudgetPolicy` | Account ID, environment, currency, initial capital, policy version, last reconciliation cursor; immutable initialization parameters | account, environment |
| `TradeIntent` and `Reservation` | Requested operation, normalized parameters, preview expiry and broker-state fingerprint; submit key, full notional plus cost buffer, expiry and terminal resolution; references to account and target | account, state, key, expiry |
| `BrokerOrder` and `Position` | Broker IDs, parent intent, symbol, direction, initial leverage/notional, fills, entry cost basis, current TP/SL, remaining exposure, origin and last observed broker version | account, symbol, state, broker ID |
| `AuditEvent` and specialized event subclasses | Immutable sequence, UTC time, actor/request/key, intent/position/order references, before/after facts, source (`cli`/`broker_manual`/`reconcile`), policy decision, broker request/response metadata with secrets redacted, and previous-event hash | account, time, operation, entity, source |
| `LedgerEntry`, `AccountSnapshot`, `PortfolioSnapshot` | Immutable principal release/reservation, net realized P&L and costs; observed broker cash/positions; account-wide and CLI-managed valuations with quote timestamp, currency, completeness and source | account, time, event, scope |

For example, these are regular Python classes whose fields can reference other memos; the environment-specific prefix is logical, while `db0.init(dbzero_root=...)` selects the physical root:

```python
import dbzero as db0

DATA_PREFIX = "/trader/production/data"

@db0.memo(prefix=DATA_PREFIX, singleton=True)
class TraderAccount:
    def __init__(self, broker_account_id, initial_balance):
        self.broker_account_id = broker_account_id
        self.initial_balance = initial_balance

@db0.memo(prefix=DATA_PREFIX, immutable=True)
class AuditEvent:
    def __init__(self, account, intent, kind, occurred_at):
        self.account = account       # Reference to TraderAccount memo
        self.intent = intent         # Reference to TradeIntent memo
        self.kind = kind
        self.occurred_at = occurred_at

# When constructing an event within the audited write path:
# db0.tags(event).add(["AUDIT", "OPEN"])
```

Use inheritance for operation-specific events (for example, open, fill, modify, cancel, close, manual override) while keeping the common audit fields/query interface in `AuditEvent`. Verify the installed dbzero memo-inheritance behavior before writing subclasses; if that version cannot persist inherited memo fields safely, persist one immutable memo event type with a discriminated `kind` and retain the same public event contract. References and tags link every event to the initiating intent and broker entity. Broker IDs and submit keys must be indexed uniquely within the account by the service; never treat a mutable display symbol as an identity.

Audit records and accounting entries are append-only; status is derived from events or maintained as a rebuildable projection. Do not edit/delete audit events to correct mistakes: append a correction event linked to the original. Use dbzero immutable memos where the installed version supports them, a monotonic per-account sequence, a hash chain over canonical redacted event content, backups and periodic externally retained chain digests if stronger tamper evidence is required. A local hash chain detects accidental or selective changes but alone cannot protect against a privileged operator rewriting the entire database. Keep dbzero-generated backup files within the environment root. Archive/export formats, if added, must not leak secrets. Document backup, restore, retention, chain verification, and replay of projections as operational procedures.

The boundary around a broker call is **not** a database transaction. First, under the account's serialized budget lock, re-read the latest broker state, check the policy, create the command/reservation and append an `INTENT_COMMITTED` event in one dbzero atomic unit, then explicitly `db0.commit()` and confirm success **before** sending a broker mutation. Next submit at most once per unique key through the adapter; record the broker acknowledgment, fill, rejection, or timeout and commit the resulting immutable event(s) and ledger entries. Do not rely on dbzero's periodic autocommit for either durability barrier. If the process crashes or the network times out after dispatch, mark the outcome `UNKNOWN`, query broker orders/positions/history by available broker IDs, client reference and timestamps, and reconcile before deciding whether to retry. If the broker cannot support safe identification of an ambiguous order, quarantine it and block conflicting new risk; never blindly resubmit. Rebuild reservations after restart and reconcile before accepting further exposure. Record rejected attempts and unsupported actions as audit events after initialization as well.

## Budget and broker-state rules

The budget is a virtual allocation within a shared account, not a separate eToro wallet. All amounts below are in the account base currency; convert broker amounts using recorded rates and fees, and report stale/missing rates as an inability to place additional risk. Existing manual positions are not counted as CLI-owned, and their profits or external cash movements never augment the allocation.

```text
capital_ceiling = initial_capital + settled_net_realized_pnl_on_cli_positions
committed = sum(remaining entry notional of CLI-owned open positions)
          + sum(unfilled full notional of CLI-owned pending orders)
          + sum(full notional of in-flight opening reservations)
          + sum(unsettled estimated charges on pending/in-flight opens)
available_to_open = max(0, capital_ceiling - committed)
```

`settled_net_realized_pnl` includes gains **and losses**, commissions/spread/financing/FX costs actually attributable to CLI-owned positions; it is not just a cumulative sum of wins. Charge posted entry costs immediately against the capital ceiling; reserve estimated costs until their actual amount posts, without counting both. Include a conservative estimate of charges and FX/slippage in each reservation, even though the headline cap is full notional. For a leveraged open, reserve leveraged **notional**, not broker margin. Short-sale proceeds and unrealized gains do not create spendable capital. A partial close releases its fraction of original committed notional and records only that fraction's settled net P&L; a full close releases the remainder. An unfilled canceled order releases its reservation; partial fills exchange the corresponding pending reservation for position exposure without double counting. A modification that increases notional must pass the same budget check; decreasing risk or closing remains permitted even when the allocation is over cap. Profit realized by a manual close of a CLI-owned position counts once after verified settlement; profit on unrelated manual positions never counts. Broker free cash/margin and instrument eligibility are **additional** limits: passing the virtual budget never guarantees the broker will accept an order. If the broker cannot constrain a submitted order's maximum notional under slippage and quantity rounding, reject that order type rather than risk silently exceeding the cap.

For example, with USD 1,000 initial capital, a 2x leveraged USD 400-notional opening commits USD 400, not its approximate USD 200 margin. Another USD 600 of opening notional is possible only if broker availability and cost buffers permit it. Closing the first position for a settled USD 20 net gain releases its USD 400 commitment and raises the capital ceiling to USD 1,020; closing it for a USD 20 net loss instead lowers the ceiling to USD 980. Reinvested profits have no independent lifetime limit, but every new trade remains subject to the current ceiling and real broker availability.

This account is shared. Reconcile account and position state before every preview and again immediately before submit, and periodically while orders are pending. Manual eToro changes **supersede** a CLI plan: auto-adopt the latest verified state of a CLI-created position/order, append `MANUAL_OVERRIDE` with the observed delta, invalidate affected previews, and calculate the new reservation/P&L from broker facts. A manually closed position must not be reopened by an old order or retry. Unrelated manual trades are visible in account-wide audits but do not become CLI-owned. If a manual upsize or account cash withdrawal leaves less room than committed CLI exposure, preserve broker state, block new openings or risk-increasing modifications, and allow closes, cancels, and other risk reductions. If attribution is ambiguous (for example, broker IDs changed or a fill cannot be uniquely matched), quarantine the affected entity and fail closed for new risk until reconciliation can resolve it. Never reverse a user's manual broker action automatically.

## Public Python and CLI contracts

`TraderService(environment)` exposes `initialize(balance, currency)`, `capabilities()`, `account_status()`, `positions()`, `orders()`, `preview_open(...)`, `preview_modify(...)`, `preview_close(...)`, `preview_cancel(...)`, `submit(preview_id, idempotency_key)`, `intent_status(intent_id)`, `reconcile()`, `audit_events(...)`, `portfolio_history(...)`, and `trends(...)`. Public inputs/outputs are typed, JSON-serializable value objects; dbzero memo objects are internal implementation details. Opening inputs include `symbol`, `side=buy|sell`, `order_type=market|limit|stop` if supported, account-currency `notional`, `leverage`, optional trigger/limit price, optional TP/SL, and optional maximum slippage. Modification targets a stable broker position/order ID and changes only fields explicitly supported for that entity; any unsupported leverage change or pending-price edit is rejected, not simulated by close/reopen. Partial close specifies a broker-compatible quantity or fraction; the normalized broker quantity and rounding appear in the preview.

`preview_*` returns a short-lived `preview_id`, expiry, operation and target, normalized broker quantity/price, estimated notional and charges, remaining virtual and broker availability, policy warnings, and the broker-state fingerprint. Preview has **no broker effect and no permanent reservation**. `submit` must provide both preview ID and caller-generated idempotency key; it rechecks freshness, capabilities, state, cash and budget while atomically acquiring a durable reservation. Repeating the same key and parameters returns the same action/result; reusing a key with different parameters is `IDEMPOTENCY_CONFLICT`. A changed broker state or expired preview returns `STALE_PREVIEW` and requires a new preview. Submitted actions may be `PENDING`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `REJECTED`, or `UNKNOWN`; never report success just because the network request was sent.

Every CLI command accepts `--env production|development` (no implicit production default) and `--json`; agents should always use `--json`. JSON responses use a stable envelope: `{"schema_version":1,"request_id":"...","status":"ok|pending|error","data":{},"error":null}`. Errors have stable `code`, `message`, `retryable`, and `details` fields; common codes are `NOT_INITIALIZED`, `ACCOUNT_MISMATCH`, `UNSUPPORTED_CAPABILITY`, `INSUFFICIENT_BUDGET`, `INSUFFICIENT_BROKER_FUNDS`, `STALE_PREVIEW`, `IDEMPOTENCY_CONFLICT`, and `RECONCILIATION_REQUIRED`. Exit 0 for successful read/accepted submit, nonzero for a rejected request; an accepted asynchronous order returns `status=pending`, not an error. Output never contains credentials or unredacted broker tokens. Mutating commands require the preview/submit two-step; `init` is the sole exception. Do not expose a `--force`, arbitrary balance setter, or raw broker pass-through that bypasses this workflow.

Example preview response, with USD 2 in conservatively estimated costs and no other committed orders:

```json
{
  "schema_version": 1,
  "request_id": "req_001",
  "status": "ok",
  "data": {
    "preview_id": "pv_001",
    "expires_at": "2026-10-07T10:30:00Z",
    "operation": "open",
    "symbol": "AAPL",
    "notional": "400.00",
    "leverage": 2,
    "estimated_costs": "2.00",
    "remaining_budget_after_submit": "598.00",
    "broker_state_fingerprint": "sha256:...",
    "warnings": ["Stop loss does not guarantee a maximum loss"]
  },
  "error": null
}
```

An error instead has `status="error"`, `data=null`, and, for example, `"error":{"code":"STALE_PREVIEW","message":"Broker state changed; preview again","retryable":true,"details":{}}`. A submit result includes the durable intent ID and current broker order/position IDs if known; when the outcome is `UNKNOWN`, the caller checks `reconcile` or an intent-status query instead of generating a new submit key.

Illustrative production CLI flow (IDs and broker prices are placeholders, not live recommendations):

```bash
trader --env production init --balance 1000.00 --currency USD --json
trader --env production capabilities --json
trader --env production trade preview-open --symbol AAPL --side buy --order-type market \
  --notional 400.00 --leverage 2 --stop-loss-price 150 --take-profit-price 210 --json
# Read preview_id and expiry from JSON; submit only after reviewing the normalized exposure.
trader --env production trade submit --preview-id pv_001 --idempotency-key strategy42-open-001 --json
trader --env production trade status --intent-id intent_001 --json
trader --env production orders --json
trader --env production positions --json
```

Pending entry, modifications, close, and cancellation follow the same preview/submit protocol; the adapter rejects order types or modifications unsupported by this account:

```bash
trader --env production trade preview-open --symbol AAPL --side buy --order-type limit \
  --limit-price 170 --notional 200 --leverage 1 --stop-loss-price 155 --take-profit-price 195 --json
trader --env production trade submit --preview-id pv_002 --idempotency-key strategy42-limit-002 --json
trader --env production trade preview-modify --position-id pos_123 --stop-loss-price 160 --take-profit-price 205 --json
trader --env production trade submit --preview-id pv_003 --idempotency-key strategy42-modify-003 --json
trader --env production trade preview-close --position-id pos_123 --fraction 0.5 --json
trader --env production trade submit --preview-id pv_004 --idempotency-key strategy42-close-004 --json
trader --env production trade preview-cancel --order-id ord_456 --json
trader --env production trade submit --preview-id pv_005 --idempotency-key strategy42-cancel-005 --json
```

The Python interface performs the same validation and returns equivalent structured results:

```python
from decimal import Decimal
from trader_api import TraderService

trader = TraderService(environment="production")
preview = trader.preview_open(
    symbol="AAPL", side="buy", order_type="market",
    notional=Decimal("400.00"), leverage=2,
    stop_loss_price=Decimal("150"), take_profit_price=Decimal("210"),
)
result = trader.submit(preview.preview_id, idempotency_key="strategy42-open-001")
print(result.status, result.broker_order_id)
```

`trader --env production reconcile --json` imports newly observed broker fills and manual changes, resolves pending/unknown operations, and reports discrepancies. Audits are available through both interfaces:

```bash
trader --env production audit events --from 2026-10-01T00:00:00Z --json
trader --env production audit verify --json
trader --env production audit portfolio-history --scope account --from 2026-10-01T00:00:00Z --json
trader --env production audit portfolio-history --scope managed --from 2026-10-01T00:00:00Z --json
trader --env production audit trends --scope managed --period 7d --json
```

`audit_events`, `portfolio_history(scope="account"|"managed", start, end, cursor)`, and `trends(scope, period)` provide paginated UTC results, timestamps, source, base currency, and gap/completeness indicators. `account` is the broker's entire portfolio, including unrelated manual activity; `managed` is the CLI allocation and its attributed positions/P&L. Sample account value at initialization, each reconcile or transaction, and at a configured hourly cadence (for example, by invoking `trader audit snapshot` from a scheduler). Preserve broker-provided historical facts if available; do not fabricate earlier snapshots or interpolate over missing observations. Trends return start/end value, absolute/percentage change, realized/unrealized P&L where attributable, net external cash flows for account scope, and the sampling interval; distinguish investment returns from deposits/withdrawals. A missing valuation or price produces a visible gap rather than a false zero.

## Failure handling, operations, and acceptance

Use a deterministic fake broker in automated tests, with dbzero test data exclusively in isolated `/dbzero-data/trader-dev` subdirectories; the runtime development profile still requires an official demo/sandbox. Test serial and concurrent submissions, atomic reservations, repeated keys, expired and stale previews, ambiguous timeouts, restart/replay, partial fills and closes, rounding/FX/fees, over-cap manual edits, unavailable funds, account mismatch, unsupported capabilities, and fail-closed sandbox configuration. Never run automated tests against a real-money account. Verify that explicit commits precede outbound mutations and that every terminal/unknown transition has a durable linked audit event. Validate portfolio history/trend gaps, account versus managed scope, redaction, chain verification, and backup/restore replay.

Release readiness requires a written mapping from each enabled CLI operation to **verified** official broker capability and an integration test on a non-live account. Then verify the production account ID and initial cash before the one-time initialization; start with read-only reconciliation and operator-reviewed small orders. Alert on unresolved `UNKNOWN` actions, attribution gaps, chain-verification failures, over-cap exposure, stale prices, and mismatch between broker state and memo projections. Recovery must reconcile broker facts before reopening trading; disabling or removing the CLI never implies canceling a user's manual trades.

References for implementers: `/src/selltime/AGENTS.md` for local dbzero model/query conventions; [dbzero transactions](https://docs.dbzero.io/transactions) and [atomic operations](https://docs.dbzero.io/api-reference/atomic) for explicit-commit and atomic-block semantics. Confirm the installed `dbzero-pro` API and official eToro documentation before turning the conceptual model into runnable integration code.
