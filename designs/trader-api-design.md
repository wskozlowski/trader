# Agentic eToro trading CLI and Python API

## Purpose and boundaries

`trader` is a Python service library with a machine-readable command-line interface for multiple mutually isolated agents (called **traders**) that trade one **real-money eToro account**. Each trader's first state-changing command establishes that trader's fixed amount of initial capital. Each trader has an independent balance, portfolio, orders, audit history, and dbzero data partition; no trader can view, reference, spend, modify, or infer another trader's state. Subsequent trading can recycle that trader's returned principal and spend that trader's settled, realized net profits without a separate profits cap. Full leveraged notional, not merely margin, counts against the owning trader's available capital.

The supported workflow is opening long or short positions at market or by pending order, setting take-profit (TP) and stop-loss (SL), selecting leverage at open, modifying supported position/order settings, partially or fully closing positions, and canceling pending orders. Copy trading, deposits, withdrawals, transfers, and other account administration are **out of scope**. Every action the broker actually supports must be exposed consistently through the Python API and CLI. Availability depends on the account, instrument, jurisdiction, and authenticated official eToro API: there must be no screen scraping, private endpoints, fictitious broker responses, or automatic conversion of an unsupported action into a different trade.

This document is a design, not a claim that a particular eToro endpoint, demo environment, or `dbzero-pro` version has been verified. Before implementation, validate the approved integration's authentication, account binding, allowed order types, TP/SL units, modification and partial-close capabilities, idempotency/reconciliation identifiers, rate limits, and demo-account support. Publish discovered capabilities through `trader capabilities`; reject unsupported operations with `UNSUPPORTED_CAPABILITY` before accepting a preview. If an official demo endpoint cannot be verified, demo trading fails closed; it must never fall back to the real-money account.

This budget limits **CLI-initiated opening notional**, not the account's worst-case loss. Gaps, leverage, fees, liquidation, manual account activity, and broker execution may cause losses beyond the configured initial amount. SL and TP are instructions to the broker, not guarantees of execution price. No trade occurs merely because an agent has requested a preview.

## Runtime, trust boundary, and storage

The executable and importable library share one `TraderService` implementation and one account-level execution coordinator with a validated broker adapter; the CLI is not a second trading implementation. A trader authenticates with a service credential bound server-side to exactly one immutable `trader_id`; a CLI `--trader` value is only an explicit routing assertion and must match the authenticated identity. Traders never receive eToro credentials or direct access to the coordinator. Use `Decimal` for money, UTC timestamps, the eToro account's reported base currency as the budget currency, and explicit rounding up for estimated costs/reservations. Never use binary floating-point arithmetic for budget decisions. Broker and service configuration comes from the selected environment's fixed dotenv file, described below, and never appears in command arguments, JSON output, memo objects, or audit payloads. Agent callers may invoke only their constrained service API, not arbitrary dbzero mutations, other prefixes, the control prefix, or the raw broker client.

The coordinator is the sole live-account writer and serializes account-level broker submissions, ownership claims, and capacity checks across all traders and manual account activity. It may know that traders exist and maintain the minimum routing data needed for safety, but it never exposes one trader's identity, capital, positions, results, capacity consumption, or errors to another. Trader-facing errors such as `BROKER_CAPACITY_UNAVAILABLE` reveal no account totals or competing activity. A separate administrator role may inspect account-wide coordination and reconciliation data; trader credentials cannot use that interface.

In real mode, run the coordinator and each trader data worker in separate service security contexts. A trader worker opens only its assigned prefix read-write; the coordinator opens only the control prefix read-write and passes immutable scalar commands/results to the correct worker. An administrator's aggregation process opens trader prefixes read-only. This makes the prefix boundary enforceable rather than depending on every query remembering a trader filter.

| Environment | Configuration file | Required dbzero root | Broker identity |
| --- | --- | --- | --- |
| `real` | `.env` | `/dbzero-data/trader` | Explicitly configured and verified real-money eToro account |
| `demo` (default) | `.env_demo` | `/dbzero-data/trader-dev` | Explicitly configured and verified official demo/sandbox account |

Resolve the environment before reading configuration. If `--env` is absent, resolve `demo` and load exactly `.env_demo`; if the command contains the literal `--env real`, load exactly `.env`. Locate both files from the fixed application/project root, never by searching the caller's current working directory. Do not merge the files, load both, inherit missing values from the other file, or fall back from a missing/invalid `.env_demo` to `.env`. Missing, unreadable, duplicate, malformed, or mismatched configuration fails closed before dbzero or the broker client is opened.

Both files use the same validated settings schema for the broker API URL, account identifier, API credentials, service-authentication settings, timeouts, and other account-level configuration. Each must contain an explicit environment marker (`TRADER_ENV=real` in `.env`, `TRADER_ENV=demo` in `.env_demo`) and its expected account identifier; startup authenticates the broker and verifies both against the selected environment. The dbzero root is fixed by the table above and cannot be overridden from dotenv, process environment, or a CLI flag. Safety-critical settings—including environment, endpoint, account ID, and dbzero root—must not be overridden by ambient process variables. Parse once at startup into an immutable typed settings object; configuration changes require a restart. Audit only a redacted configuration fingerprint and version, never values or credentials.

Treat `.env` and `.env_demo` as secrets: keep them ignored by Git, owned by the service account, and readable/writable only by that account (normally mode `0600`). Commit only redacted `.env.example` and `.env_demo.example` templates if templates are needed. Never show file contents through diagnostics, `--json`, exceptions, process listings, or audit exports. The files configure the shared eToro account and service; per-trader balances, portfolios, registrations, and authorization bindings remain in their isolated dbzero/control partitions rather than in dotenv.

Configure dbzero's **physical root** as the corresponding absolute path. Inside it, create one control prefix and one dedicated prefix per trader:

```text
/trader/{environment}/control
/trader/{environment}/traders/{storage_key}
```

Each prefix is a separate dbzero data partition with an independent commit history and backing dbzero file set. The trader prefix is that trader's complete view: all portfolio, order, balance, intent, ledger, snapshot, and audit memos for two different traders must never occupy the same prefix or backing data file. `storage_key` is a fixed opaque UUID or keyed digest assigned by the coordinator, never a raw user-controlled path segment; the private control prefix maps it to `trader_id`. At startup, assert that every discovered trader prefix maps to one and only one registered trader and that no trader model exists in the control prefix. Do not use cross-prefix memo references or `db0.weak_proxy()` between trader and control data; exchange immutable scalar command/result messages through the service boundary instead. Backups and exports preserve the same per-prefix isolation.

A logical prefix is not a substitute for the physical root. Every dbzero-managed file, index, journal, snapshot, and test fixture belongs inside the selected environment root (tests use isolated subdirectories under `/dbzero-data/trader-dev`). Resolve and validate paths, including symlinks, before opening the store; reject a root outside its allowlisted directory, an environment/account mismatch, or an attempt to change the initialized account. Do not rely on the working directory or a user-provided arbitrary `--db-path`. Provision the demo directory before use if it does not exist. Keep separate broker credentials and never copy real-account memo data into demo.

Only `--help`, `version`, and per-trader `init` may run before that trader is initialized. For example, `trader --env real --trader alpha init --balance 1000.00 --currency USD` authenticates `alpha`, verifies the main account and base currency, and asks the coordinator to reserve a new USD 1,000 virtual allocation. Initialization succeeds only if the authenticated identity is authorized to create that trader and the sum of all traders' initial allocations plus account-level safety reserves does not exceed allocatable broker cash at that instant. The coordinator first durably registers the allocation and unique storage key, then initializes and commits the trader's own prefix with only its trader ID, currency, starting trader snapshot, fixed initial capital, and policy version. A crash between those commits is repaired by an idempotent initialization saga before any trading is allowed.

`init` is single-use per environment/trader; retry with identical input is idempotent, while a different amount, currency, identity, or account fails. No trader may top up itself, transfer capital or positions to another trader, or reuse another trader's allocation. An external deposit is not a trader budget credit. Released allocation from a deleted or disabled trader is an explicit administrator operation only after every owned order, position, unknown outcome, and audit export has been resolved; trader deletion never deletes its audit file. Positions present before multi-trader registration remain manual/unowned. The eToro main account is configured once by an administrator and is never exposed as a trader-owned object.

## Persistent object graph and audit trail

All durable application entities are dbzero memo objects: ordinary Python classes decorated with `@db0.memo(...)`, assigned at construction to either the caller's trader prefix or the private control prefix, and using references only to objects in the same prefix. Follow the conventions in `/src/selltime/AGENTS.md`: singleton root where appropriate; `db0.tags(obj).add(...)`, `db0.find(Class, ...)`, and indexed queries instead of Python-side scanning. The following is the logical schema, not a promise of unverified decorator or broker field signatures:

| Memo type | Durable content and relationships | Useful tags/indexes |
| --- | --- | --- |
| `TraderState` singleton and `BudgetPolicy` (trader prefix) | Trader ID, environment, currency, initial capital, policy version, last trader reconciliation cursor; immutable initialization parameters; no main-account or other-trader snapshot | trader, environment |
| `TradeIntent` and `Reservation` (trader prefix) | Requested operation, normalized parameters, preview expiry and opaque broker-state fingerprint; trader-scoped submit key, full notional plus cost buffer, expiry and terminal resolution; references to local state and target | trader, state, key, expiry |
| `BrokerOrder` and `Position` (trader prefix) | Only broker entities owned by this trader: broker IDs, parent intent, symbol, direction, leverage/notional, fills, cost basis, TP/SL, remaining exposure and last observed version | symbol, state, broker ID |
| `AuditEvent` and specialized event subclasses (trader prefix) | Immutable trader-local sequence, UTC time, actor/request/key, local references, before/after facts, source (`trader`/`broker_manual`/`reconcile`), policy decision, redacted broker metadata, and previous-event hash | time, operation, entity, source |
| `LedgerEntry` and `PortfolioSnapshot` (trader prefix) | Immutable principal release/reservation, net realized P&L and costs; valuation of only this trader's cash allocation and owned broker entities | time, event |
| `AccountControl` and `TraderRegistration` (control prefix) | Main broker account binding; opaque trader/storage mapping; authorized/disabled state; initial allocation and aggregate safety accounting, without trader strategy or portfolio projections | trader hash, storage key, state |
| `OwnershipClaim` and `ControlEvent` (control prefix) | Global broker order/position ID to opaque owner mapping, account submission reservation, reconciliation cursor, command digest, state and outcome needed for serialization/recovery | broker ID, owner key, global request/key, state |

For example, these are regular Python classes whose fields can reference other memos in the same prefix. `db0.init(dbzero_root=...)` selects the physical root and `db0.set_prefix()` assigns each instance dynamically to the isolated trader partition. The service derives `trader_prefix`; callers never supply it:

```python
import dbzero as db0

@db0.memo(singleton=True)
class TraderState:
    def __init__(self, trader_prefix, trader_id, initial_balance):
        db0.set_prefix(self, trader_prefix)
        self.trader_id = trader_id
        self.initial_balance = initial_balance

@db0.memo(immutable=True)
class AuditEvent:
    def __init__(self, trader_prefix, trader, intent, kind, occurred_at):
        db0.set_prefix(self, trader_prefix)
        self.trader = trader         # Same-prefix TraderState reference
        self.intent = intent         # Same-prefix TradeIntent reference
        self.kind = kind
        self.occurred_at = occurred_at

# When constructing an event within the audited write path:
# db0.tags(event).add(["AUDIT", "OPEN"])
```

Use inheritance for operation-specific events (for example, open, fill, modify, cancel, close, manual override) while keeping the common audit fields/query interface in `AuditEvent`. Verify the installed dbzero memo-inheritance and dynamic-prefix behavior before writing subclasses; if that version cannot persist either safely, persist one immutable memo event type with a discriminated `kind` and explicitly set its prefix while retaining the same public event contract. Same-prefix references and tags link every event to the initiating intent and broker entity. The control prefix stores only scalar trader/storage keys and command digests, never references into a trader prefix. Broker IDs are globally unique in the private ownership registry; idempotency keys are unique within `(trader_id, key)`. Never treat a mutable display symbol as an identity.

Audit records and accounting entries are append-only; status is derived from events or maintained as a rebuildable projection. Do not edit/delete audit events to correct mistakes: append a correction event linked to the original. Use dbzero immutable memos where the installed version supports them, a monotonic sequence and independent hash chain per trader, plus a separate control-event chain. A trader backup/export contains only that trader's prefix; account administrators may back up the control prefix separately. Periodically retain chain digests externally if stronger tamper evidence is required. A local hash chain detects accidental or selective changes but alone cannot protect against a privileged operator rewriting an entire database. Keep dbzero-generated backup files within the environment root. Archive/export formats must not leak secrets or other traders' identifiers. Document per-trader backup, restore, retention, chain verification, and replay of projections as operational procedures.

The boundary around a broker call is **not** a database transaction, and dbzero does not provide one atomic commit across isolated prefixes. Use a recoverable saga. First commit the local `TradeIntent`, trader reservation, and `INTENT_COMMITTED` event atomically in the owning trader prefix. Then, under the coordinator's serialized account lock, re-read broker state and commit a control reservation containing the opaque owner key, globally scoped command digest, and ownership claim. Only after both explicit `db0.commit()` barriers succeed may the coordinator send the broker mutation. Persist the broker acknowledgment, fill, rejection, or timeout in the control prefix first, then idempotently project that result and linked audit/ledger entries into the owner prefix. No result is ever projected to any other trader. A local intent with no control reservation is safely canceled during recovery; a control reservation/outcome missing from the trader file is replayed from the command digest and result message. Do not rely on periodic autocommit for these barriers.

If the process crashes or the network times out after dispatch, mark the control outcome and the owner's intent `UNKNOWN`, query broker orders/positions/history by available broker IDs, client reference and timestamps, and reconcile before deciding whether to retry. If the broker cannot support safe identification of an ambiguous order, quarantine it and block only the owning trader plus any account-level conflicting risk; other traders receive only a generic capacity/reconciliation error if affected. Never blindly resubmit. Rebuild local and control reservations after restart, compare them, and reconcile before accepting further exposure. Record rejected attempts and unsupported actions in the requesting trader's audit after initialization, without exposing control details.

## Budget and broker-state rules

Each budget is an independent virtual allocation within the shared account, not a separate eToro wallet. Evaluate the following formula solely from the authenticated trader's prefix; an operation can never consume another trader's unused capital or profits. All amounts are in the main account's base currency; convert broker amounts using recorded rates and fees, and report stale/missing rates as an inability to place additional risk. Existing manual positions are not trader-owned, and their profits or external cash movements never augment any trader allocation.

```text
trader_capital_ceiling = trader_initial_capital
                       + settled_net_realized_pnl_on_this_trader_positions
trader_committed = sum(remaining entry notional of this trader's open positions)
          + sum(unfilled full notional of this trader's pending orders)
          + sum(full notional of this trader's in-flight opening reservations)
          + sum(unsettled estimated charges on pending/in-flight opens)
trader_available_to_open = max(0, trader_capital_ceiling - trader_committed)
```

`settled_net_realized_pnl` includes gains **and losses**, commissions/spread/financing/FX costs actually attributable to that trader's positions; it is not just a cumulative sum of wins. Profits and losses never migrate between traders. Charge posted entry costs immediately against the owning trader's capital ceiling; reserve estimated costs until their actual amount posts, without counting both. Include a conservative estimate of charges and FX/slippage in each reservation, even though the headline cap is full notional. For a leveraged open, reserve leveraged **notional**, not broker margin. Short-sale proceeds and unrealized gains do not create spendable capital. A partial close releases its fraction of original committed notional and records only that fraction's settled net P&L; a full close releases the remainder. An unfilled canceled order releases its reservation; partial fills exchange the corresponding pending reservation for position exposure without double counting. A modification that increases notional must pass the same trader-local budget check; decreasing risk or closing remains permitted when that trader is over cap. Profit realized by a manual close of an owned position counts once for its owner; profit on manual or another trader's positions never counts.

The coordinator separately checks actual account cash/margin, total in-flight submissions, manual account activity, instrument eligibility, and the account safety reserve. This check cannot borrow or debit another trader's virtual balance and reveals only pass/fail to the requester. Passing the local allocation never guarantees broker acceptance; when aggregate account capacity is temporarily unavailable, reject with `BROKER_CAPACITY_UNAVAILABLE` and no other-trader detail. Queueing must not create hidden reservations or priority: a caller must preview again. Serialize simultaneous submit checks so two traders cannot both consume the same broker capacity. If the broker cannot constrain maximum notional under slippage and quantity rounding, reject that order type rather than risk silently exceeding a trader's cap.

For example, trader `alpha` with USD 1,000 initial capital commits USD 400 for a 2x leveraged USD 400-notional opening, not its approximate USD 200 margin. Trader `beta`'s balance is irrelevant to this calculation. Alpha can open at most another USD 600 before cost buffers, subject to actual account availability. Closing alpha's first position for a settled USD 20 net gain releases its USD 400 commitment and raises **alpha's** ceiling to USD 1,020; closing it for a USD 20 net loss lowers alpha's ceiling to USD 980. Beta's ceiling does not change. Reinvested profits have no independent lifetime limit, but every new trade remains subject to the owner's ceiling and real broker availability.

This account is shared. Reconcile account and position state before every preview and immediately before submit, and periodically while orders are pending. Every submitted order must carry a broker-supported unique client reference derived from the opaque owner key and intent ID; the private control map binds returned broker order/position IDs permanently to one trader. A trader may modify, close, or cancel only entities in its own prefix whose control ownership agrees. If eToro nets/merges positions across orders or does not expose stable IDs sufficient to preserve ownership, the coordinator must either prohibit overlapping instrument/direction combinations across traders or disable multi-trader live trading entirely; it must never estimate ownership by proportional allocation after the fact.

Manual eToro changes **supersede** a trader plan. Auto-adopt a manual change only into the verified owner's file, append that owner's `MANUAL_OVERRIDE`, invalidate only affected previews, and calculate that owner's reservation/P&L from broker facts. A manually closed position must not be reopened by an old order or retry. Unrelated manual trades appear only in the administrator's account audit and do not become visible to traders. If a manual upsize affects an owned position, debit/quarantine only its owner; if a cash withdrawal or global account restriction reduces aggregate capacity, block new/risk-increasing actions as needed but expose no competing positions. Always allow an owner to request broker-permitted closes, cancels, and other risk reductions. If ownership is ambiguous, quarantine the entity in the control prefix and fail closed for operations that could conflict; do not expose it in multiple trader files. Never reverse a user's manual broker action automatically.

## Public Python and CLI contracts

`TraderService(authenticated_identity, environment="demo")` resolves one immutable trader context and exposes `initialize(balance, currency)`, `capabilities()`, `trader_status()`, `positions()`, `orders()`, `preview_open(...)`, `preview_modify(...)`, `preview_close(...)`, `preview_cancel(...)`, `submit(preview_id, idempotency_key)`, `intent_status(intent_id)`, `reconcile()`, `audit_events(...)`, `portfolio_history(...)`, and `trends(...)`. Omitting the Python environment selects demo exactly as the CLI does; callers must pass `environment="real"` explicitly for real money. The service has no API for listing traders, changing trader context, querying the main account, or reading an arbitrary prefix. Public inputs/outputs are typed, JSON-serializable value objects; dbzero memo objects are internal. Opening inputs include `symbol`, `side=buy|sell`, `order_type=market|limit|stop` if supported, account-currency `notional`, `leverage`, optional trigger/limit price, optional TP/SL, and optional maximum slippage. Modification targets a stable broker position/order ID owned by the caller and changes only fields explicitly supported for that entity; a foreign/unknown ID returns the same `NOT_FOUND` response. Any unsupported leverage change or pending-price edit is rejected, not simulated by close/reopen. Partial close specifies a broker-compatible quantity or fraction; the normalized broker quantity and rounding appear in the preview.

`preview_*` returns a short-lived trader-scoped `preview_id`, expiry, operation and target, normalized broker quantity/price, estimated notional and charges, the caller's remaining virtual balance, policy warnings, and an opaque state fingerprint. It does not return main-account totals, aggregate broker availability, or data from another trader. Preview has **no broker effect and no permanent reservation**. `submit` must provide both preview ID and caller-generated idempotency key; it rechecks freshness, ownership, capabilities, local budget and global broker capacity while acquiring the two durable saga reservations. Repeating the same `(trader_id, key)` and parameters returns the same action/result; the same textual key used by a different trader is unrelated. Reusing a key within one trader with different parameters is `IDEMPOTENCY_CONFLICT`. A changed relevant broker state or expired preview returns `STALE_PREVIEW` and requires a new preview. Submitted actions may be `PENDING`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `REJECTED`, or `UNKNOWN`; never report success just because the network request was sent.

Every trader CLI command accepts `--env demo|real`, `--trader TRADER_ID`, and `--json`. The accepted environment names are exactly `demo` and `real`; do not support `prod`, `production`, `dev`, or `development` aliases. Omitting `--env` always selects `demo`; configuration files and environment variables must not silently change that default to `real`. Access to the real-money account therefore always requires the literal `--env real` on that invocation. There is no implicit trader: `--trader` is required, must equal the authenticated identity, and is included to prevent operator mistakes rather than grant access. JSON responses include the resolved environment and use a stable envelope: `{"schema_version":1,"request_id":"...","environment":"demo","trader_id":"alpha","status":"ok|pending|error","data":{},"error":null}`. Errors have stable `code`, `message`, `retryable`, and `details` fields; common codes are `NOT_INITIALIZED`, `TRADER_MISMATCH`, `ACCOUNT_MISMATCH`, `NOT_FOUND`, `UNSUPPORTED_CAPABILITY`, `INSUFFICIENT_BUDGET`, `BROKER_CAPACITY_UNAVAILABLE`, `STALE_PREVIEW`, `IDEMPOTENCY_CONFLICT`, and `RECONCILIATION_REQUIRED`. Exit 0 for successful read/accepted submit, nonzero for a rejected request; an accepted asynchronous order returns `status=pending`, not an error. Output never contains credentials, storage keys, control identifiers, account totals, another trader's data, or unredacted broker tokens. Mutating commands require preview/submit; `init` is the sole exception. Do not expose `--force`, context switching, arbitrary balance setters, transfers, raw prefix access, or raw broker pass-through.

Example preview response, with USD 2 in conservatively estimated costs and no other committed orders:

```json
{
  "schema_version": 1,
  "request_id": "req_001",
  "environment": "real",
  "trader_id": "alpha",
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

Demo is the default when `--env` is absent:

```bash
trader --trader alpha init --balance 1000.00 --currency USD --json
trader --trader alpha capabilities --json
trader --trader alpha positions --json
```

Each response above contains `"environment":"demo"` and uses `/dbzero-data/trader-dev`. A typo or omitted `--env` can therefore never select the real account.

Illustrative real-money CLI flow for trader `alpha` (IDs and broker prices are placeholders, not live recommendations). Real money is selected explicitly on every invocation:

```bash
trader --env real --trader alpha init --balance 1000.00 --currency USD --json
trader --env real --trader alpha capabilities --json
trader --env real --trader alpha trade preview-open --symbol AAPL --side buy --order-type market \
  --notional 400.00 --leverage 2 --stop-loss-price 150 --take-profit-price 210 --json
# Read preview_id and expiry from JSON; submit only after reviewing the normalized exposure.
trader --env real --trader alpha trade submit --preview-id pv_001 --idempotency-key strategy42-open-001 --json
trader --env real --trader alpha trade status --intent-id intent_001 --json
trader --env real --trader alpha orders --json
trader --env real --trader alpha positions --json
```

Pending entry, modifications, close, and cancellation follow the same preview/submit protocol; the adapter rejects order types or modifications unsupported by this account:

```bash
trader --env real --trader alpha trade preview-open --symbol AAPL --side buy --order-type limit \
  --limit-price 170 --notional 200 --leverage 1 --stop-loss-price 155 --take-profit-price 195 --json
trader --env real --trader alpha trade submit --preview-id pv_002 --idempotency-key strategy42-limit-002 --json
trader --env real --trader alpha trade preview-modify --position-id pos_123 --stop-loss-price 160 --take-profit-price 205 --json
trader --env real --trader alpha trade submit --preview-id pv_003 --idempotency-key strategy42-modify-003 --json
trader --env real --trader alpha trade preview-close --position-id pos_123 --fraction 0.5 --json
trader --env real --trader alpha trade submit --preview-id pv_004 --idempotency-key strategy42-close-004 --json
trader --env real --trader alpha trade preview-cancel --order-id ord_456 --json
trader --env real --trader alpha trade submit --preview-id pv_005 --idempotency-key strategy42-cancel-005 --json
```

The Python interface performs the same validation and returns equivalent structured results:

```python
from decimal import Decimal
from trader_api import TraderService

trader = TraderService(environment="real", authenticated_identity=alpha_identity)
preview = trader.preview_open(
    symbol="AAPL", side="buy", order_type="market",
    notional=Decimal("400.00"), leverage=2,
    stop_loss_price=Decimal("150"), take_profit_price=Decimal("210"),
)
result = trader.submit(preview.preview_id, idempotency_key="strategy42-open-001")
print(result.status, result.broker_order_id)
```

`trader --env real --trader alpha reconcile --json` asks the coordinator to reconcile broker fills and manual changes relevant to alpha, resolves alpha's pending/unknown operations, and reports only alpha's discrepancies. Audits are available through both interfaces:

```bash
trader --env real --trader alpha audit events --from 2026-10-01T00:00:00Z --json
trader --env real --trader alpha audit verify --json
trader --env real --trader alpha audit portfolio-history --from 2026-10-01T00:00:00Z --json
trader --env real --trader alpha audit trends --period 7d --json
```

`audit_events`, `portfolio_history(start, end, cursor)`, and `trends(period)` provide paginated UTC results, timestamps, source, account base currency, and gap/completeness indicators for exactly the authenticated trader. The trader portfolio is its virtual uncommitted balance plus marked value of its owned positions; it excludes all other traders and unrelated manual activity. Sample it at initialization, each relevant reconcile or transaction, and at a configured hourly cadence. Preserve broker-provided historical facts if available; do not fabricate earlier snapshots or interpolate over missing observations. Trends return trader-local start/end value, absolute/percentage change, realized/unrealized P&L, attributable costs, and sampling interval. A missing valuation or price produces a visible gap rather than a false zero.

Account-wide portfolio history, manual activity, aggregate capacity, control-chain verification, and cross-trader operational health exist only in a separately authenticated `trader-admin` interface and the control prefix. An administrator may request a per-trader export, but ordinary traders cannot enumerate traders or select `scope=account`. Administrative aggregation reads isolated projections and must not write derived data back into any trader file.

## Failure handling, operations, and acceptance

Use a deterministic fake broker in automated tests, with dbzero test data exclusively in isolated `/dbzero-data/trader-dev` subdirectories; the runtime demo profile still requires an official demo/sandbox. Test at least two traders with different allocations and overlapping symbols. Prove that each gets a distinct prefix/backing file, cannot query or mutate the other's memos, IDs, previews, idempotency keys, balance, positions, audit history, trends, storage key, or error details, and cannot pass another trader ID with its credential. Inspect persisted files to ensure no trader memo appears in another trader or control partition and verify per-trader backup/restore independently.

Also test that an omitted environment resolves to demo and reads only `.env_demo`, while real money requires explicit `--env real` and reads only `.env`. Prove there is no cross-file merge or fallback, ambient variables cannot override safety-critical settings, legacy environment names are rejected, mismatched `TRADER_ENV`/account/endpoint fails before opening dbzero or making a broker request, and logs/errors never expose dotenv secrets. Test serial and simultaneous submissions across traders, aggregate account-capacity serialization, initialization allocation races, identical textual idempotency keys in different traders, expired/stale previews, cross-prefix saga crashes at every commit boundary, ambiguous timeouts, restart/replay, manual edits routed to the correct owner, foreign IDs returning `NOT_FOUND`, partial fills/closes, rounding/FX/fees, over-cap manual edits, unavailable capacity, account mismatch, unsupported capabilities, and fail-closed demo configuration. Test broker netting/ID ambiguity and confirm multi-trader trading is rejected rather than misattributed. Never run automated tests against real money. Verify explicit commits precede outbound mutations and every terminal/unknown transition reaches the owner file with a durable linked audit event. Validate trader-local portfolio history/trend gaps, administrator-only account scope, redaction, independent hash chains, and backup/restore replay.

Release readiness requires a written mapping from each enabled CLI operation to **verified** official broker capability and an integration test with multiple isolated traders on a demo account. Confirm stable per-order/position identifiers and no ownership-destroying netting before enabling multi-trader real-money trading. Then verify the real account ID and allocatable cash before initializing the first real trader; start with read-only reconciliation and operator-reviewed small orders. Alert administrators—without leaking details to traders—on unresolved `UNKNOWN` actions, ownership gaps, partition violations, chain-verification failures, aggregate over-cap exposure, stale prices, and mismatch between broker state, control records, and trader projections. Recovery must reconcile broker facts and replay incomplete cross-prefix sagas before reopening trading. Disabling a trader preserves its data and never implies canceling manual trades or releasing its allocation while owned risk remains.

References for implementers: `/src/selltime/AGENTS.md` for local dbzero model/query conventions; [dbzero prefixes](https://docs.dbzero.io/prefixes) for isolated partitions and cross-prefix reference rules; [dbzero transactions](https://docs.dbzero.io/transactions) and [atomic operations](https://docs.dbzero.io/api-reference/atomic) for explicit-commit and atomic-block semantics. Confirm the installed `dbzero-pro` API, its physical prefix-to-file layout, and official eToro documentation before turning the conceptual model into runnable integration code.
