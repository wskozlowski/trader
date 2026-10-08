# Trader API manual

This manual describes the repository's `trader_api` Python package and command-line tools, and the
direct eToro demo-account workflow validated on 2026-10-07.

## Choose the correct operating mode

There are two different workflows. They must not be confused.

| Workflow | Where positions appear | Real-balance effect | Supported interface |
| --- | --- | --- | --- |
| Direct demo trading | The account's ordinary **Virtual Portfolio** | None | eToro demo REST API |
| Agent Portfolio trading | The isolated Agent Portfolio strategy account and its copy mirrors | Creating the Agent Portfolio requires a creator investment from the main account | `trader`, `trader-admin`, and `TraderService` |

Use direct demo trading for development and live integration testing without affecting real money.
The current `trader` CLI is built around a previously provisioned Agent Portfolio binding; it is not
a CLI for arbitrary positions in the account's ordinary Virtual Portfolio.

> **Real-money warning:** `trader-admin provision` calls eToro's Agent Portfolio creation endpoint.
> Its `--investment-usd` value is deducted from the caller's main account and placed into the
> creator's copy mirror. Demo child-token scopes do not make this allocation virtual. Do not use
> `provision` for demo-only testing.

The public eToro API currently documents demo trading and demo copying, but it does not document a
demo-only Agent Portfolio creation endpoint.

## Installation

The project requires Python 3.13 and uses the lock file committed to the repository.

```bash
uv sync --all-extras --python /usr/bin/python3.13
.venv/bin/trader --help
.venv/bin/trader-admin --help
```

The installed entry points are:

- `trader`: isolated Agent Portfolio trader CLI.
- `trader-admin`: owner-only provisioning and mirror-accounting CLI.
- `trader-worker`: authenticated local IPC worker.

## Configuration

Configuration is loaded from exactly one project-root profile. Ambient environment variables are
not merged into it. The file must be private:

```bash
chmod 600 .env_demo
```

For ordinary demo-account trading, generate an eToro key with **Environment: Demo** and
**Permission: Write**. Do not use an Agent Portfolio child token.

A demo profile uses full operation URLs:

```dotenv
ETORO_API_KEY=<application-key>
ETORO_USER_KEY=<demo-write-user-key>
ETORO_PNL_URL=https://public-api.etoro.com/api/v1/trading/info/demo/pnl
ETORO_OPEN_ORDER_URL=https://public-api.etoro.com/api/v2/trading/execution/demo/orders
ETORO_ORDER_LOOKUP_URL=https://public-api.etoro.com/api/v2/trading/info/demo/orders:lookup
ETORO_CANCEL_ORDER_URL=https://public-api.etoro.com/api/v2/trading/execution/demo/orders/{orderId}
ETORO_CLOSE_POSITION_URL=https://public-api.etoro.com/api/v1/trading/execution/demo/market-close-orders/positions/{positionId}
ETORO_CLOSE_ORDER_LOOKUP_URL=https://public-api.etoro.com/api/v1/trading/info/demo/close-orders/{orderId}
ETORO_CANCEL_CLOSE_ORDER_URL=https://public-api.etoro.com/api/v1/trading/execution/demo/market-close-orders/{orderId}
ETORO_MODIFY_POSITION_URL=https://public-api.etoro.com/api/v2/trading/demo/positions/{positionId}
ETORO_TRADING_HISTORY_URL=https://public-api.etoro.com/api/v1/trading/info/trade/demo/history
ETORO_TRADING_COSTS_URL=https://public-api.etoro.com/api/v2/trading/info/demo/costs
ETORO_TRADING_ELIGIBILITY_URL=https://public-api.etoro.com/api/v2/trading/info/demo/eligibility
ETORO_MARKET_RATES_URL=https://public-api.etoro.com/api/v1/market-data/instruments/rates
TRADER_HTTP_TIMEOUT_SECONDS=10
```

Agent Portfolio child profiles additionally contain `TRADER_SERVICE_CREDENTIAL`. Owner profiles use
`TRADER_OWNER_SERVICE_CREDENTIAL` and `TRADER_VAULT_MASTER_KEY`; `trader-admin bootstrap` can generate
those two local secrets.

`TRADER_ENV` is obsolete and rejected. The service derives an Agent Portfolio environment from
owner-retained scope evidence. `--expected-environment` is only an equality assertion; it never
selects credentials or rewrites URLs.

## `trader` CLI

### General form

```bash
.venv/bin/trader \
  --config .env_demo_child \
  --trader <immutable-trader-id> \
  --expected-environment demo \
  <command>
```

Except for `version`, `--trader` is required. Every invocation emits one JSON envelope:

```json
{
  "schema_version": 1,
  "request_id": "<uuid>",
  "environment": "demo",
  "trader_id": "<immutable-trader-id>",
  "status": "ok",
  "data": {},
  "error": null
}
```

Errors use a nonzero exit status and put `code`, `message`, `retryable`, and `details` under
`error`. Credentials and raw broker responses are not included.

### Read and lifecycle commands

| Command | Purpose |
| --- | --- |
| `version` | Print the package CLI version. |
| `capabilities` | Report documented, configured, and effectively enabled operations. |
| `status` | Report lifecycle, scope status, strategy budget, owner-mirror budget, and copy health. |
| `portfolio` | Show the bound Agent Portfolio identifiers and binding metadata. |
| `init --expected-investment AMOUNT [--currency USD]` | Activate an existing owner-provisioned binding. It does not create or fund a portfolio. |
| `positions` | List positions owned by this isolated trader prefix. |
| `orders` | List pending or acknowledged orders owned by this trader. |
| `reconcile` | Reconcile durable intents and the child strategy snapshot. Owner mirror reconciliation remains separate. |
| `audit [--limit N]` | Read the trader audit chain. |
| `verify-audit` | Verify the trader audit chain. |
| `history [--limit N]` | Return separate strategy and owner-mirror series. |
| `trends [--limit N]` | Return derived history trends. |

Examples:

```bash
.venv/bin/trader version

.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo capabilities

.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo positions

.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo reconcile
```

### Trading commands

Trading is a two-step preview/submit workflow. A preview performs no broker mutation and expires
after five minutes. Submission rechecks the binding, policy, copy health, budgets, and broker-state
fingerprint.

Open a position:

```bash
.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo trade preview-open \
  --symbol BTC --side long --order-type market \
  --strategy-notional-usd 100 --leverage 1

.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo trade submit \
  --preview-id <preview-id> --idempotency-key alpha-btc-open-001
```

Available opening arguments:

- Exactly one of `--symbol` or `--instrument-id`.
- `--side long|short`.
- `--order-type market|market_if_touched|limit_ioc`.
- `--strategy-notional-usd AMOUNT` and optional `--leverage` (default `1`).
- `--trigger-rate` for `market_if_touched`.
- `--limit-rate` for `limit_ioc`.
- Optional `--stop-loss-rate` and `--take-profit-rate`; leveraged openings require a stop loss.

`market_if_touched` can remain pending, but it becomes a market order when triggered. `limit_ioc`
is immediate-or-cancel, not a resting limit order, and its rate must be within 10% of the current
market price.

Close all or part of a position:

```bash
.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo trade preview-close \
  --position-id <position-id> --fraction 1
```

Change protection:

```bash
.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo trade preview-modify \
  --position-id <position-id> \
  --stop-loss-rate 75000 --take-profit-rate 95000 --stop-loss-type rate
```

Cancel a pending opening order:

```bash
.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo trade preview-cancel --order-id <order-id>
```

Submit any preview and inspect its durable intent:

```bash
.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo trade submit \
  --preview-id <preview-id> --idempotency-key <unique-stable-key>

.venv/bin/trader --config .env_demo_child --trader alpha \
  --expected-environment demo trade status --intent-id <intent-id>
```

An idempotency key is scoped to the environment, trader, immutable binding, and command. Reusing it
for a different preview returns `IDEMPOTENCY_CONFLICT`.

### Obsolete arguments

- `--env`: use `--expected-environment` as an assertion.
- `--balance`: use `init --expected-investment` only for an existing approved binding.
- `--notional`: use `--strategy-notional-usd`.

## `trader-admin` CLI

The admin CLI is a separate owner-credential interface.

```bash
.venv/bin/trader-admin --config .env_demo bootstrap
.venv/bin/trader-admin --config .env_demo list
```

`bootstrap` only adds local service/vault secrets, and `list` is read-only.

The following command is financially consequential even when the selected profile contains demo
trading URLs:

```bash
# REAL-BALANCE ALLOCATION: do not use for demo-only testing.
.venv/bin/trader-admin --config <owner-profile> provision \
  --trader <immutable-trader-id> \
  --investment-usd <real-allocation> \
  --portfolio-name <6-to-10-character-name> \
  --token-name <token-name> \
  --administrative-request-key <durable-key> \
  --child-config .env_demo_child
```

eToro requires `investmentAmountInUsd` during Agent Portfolio creation and deducts it from the
caller's main balance to establish the creator's copy mirror. The Agent Portfolio receives a
separate virtual strategy balance. The scopes assigned to its child token do not change the funding
source of the creator mirror.

`reconcile-mirror` records independently observed owner-mirror facts in local accounting; it does
not place a trade:

```bash
.venv/bin/trader-admin --config <owner-profile> reconcile-mirror \
  --trader alpha \
  --actual-realized-pnl-usd 0 \
  --actual-committed-usd 0 \
  --copy-healthy
```

Never mark a mirror healthy from proportional estimates. Use only actual broker observations.

## Python API

The supported package exports are:

```python
from trader_api import (
    Environment,
    OwnerAdminService,
    ScopeEvidence,
    ScopeVerifier,
    TraderError,
    TraderService,
)
```

Constructing the trader service binds one authenticated trader identity:

```python
from trader_api import TraderService

service = TraderService(
    authenticated_identity="alpha",
    config_profile=".env_demo_child",
    expected_environment="demo",
)

print(service.capabilities())
print(service.trader_status())
print(service.positions())
```

Public `TraderService` methods correspond to the CLI:

- Readiness: `capabilities()`, `trader_status()`, `portfolio()`.
- Activation: `initialize(expected_investment, currency="USD")`.
- Trading: `preview_open(...)`, `preview_close(...)`, `preview_modify(...)`,
  `preview_cancel(...)`, `submit(preview_id, idempotency_key)`, `intent_status(intent_id)`.
- State: `positions()`, `orders()`.
- Recovery/reporting: `reconcile()`, `audit_events(limit=100)`, `verify_audit()`,
  `portfolio_history(limit=100)`, `trends(limit=100)`.

Example:

```python
preview = service.preview_open(
    symbol="BTC",
    side="long",
    order_type="market",
    strategy_notional_usd="100.00",
    leverage=1,
)

intent = service.submit(
    preview_id=str(preview["preview_id"]),
    idempotency_key="alpha-btc-open-001",
)
print(service.intent_status(str(intent["intent_id"])))
```

This Python API has the same Agent Portfolio binding requirement as `trader`; it does not adopt
unrelated positions from the ordinary Virtual Portfolio.

## Direct eToro demo REST API

This is the recommended interface for testing trading behavior without touching the real balance.
All authenticated requests require these headers:

```text
x-api-key: <application key>
x-user-key: <demo/write user key>
x-request-id: <new UUID for this request>
accept: application/json
```

Never log the first two values. Use a new request UUID for each operation.

### Demo operations

| Operation | Method and endpoint |
| --- | --- |
| Portfolio/P&L | `GET /api/v1/trading/info/demo/pnl` |
| Eligibility and minimums | `POST /api/v2/trading/info/demo/eligibility` |
| Cost estimate | `POST /api/v2/trading/info/demo/costs` |
| Open order | `POST /api/v2/trading/execution/demo/orders` |
| Order lookup | `GET /api/v2/trading/info/demo/orders:lookup?orderId=...` |
| Cancel opening order | `DELETE /api/v2/trading/execution/demo/orders/{orderId}` |
| Close position | `POST /api/v1/trading/execution/demo/market-close-orders/positions/{positionId}` |
| Close-order lookup | `GET /api/v1/trading/info/demo/close-orders/{orderId}` |
| Cancel close order | `DELETE /api/v1/trading/execution/demo/market-close-orders/{orderId}` |
| Modify protection | `PATCH /api/v2/trading/demo/positions/{positionId}` |
| Trade history | `GET /api/v1/trading/info/trade/demo/history` |
| Market rates | `GET /api/v1/market-data/instruments/rates` |

Always call eligibility before opening an unfamiliar instrument. It supplies the instrument ID,
minimum position exposure, permitted direction/leverage combinations, and `settlementType`.

Eligibility request used for the working example:

```json
{
  "symbols": ["BTC", "ETH"],
  "currency": "USD"
}
```

The response confirmed:

- BTC instrument `100000`, ETH instrument `100001`.
- Long leverage-one trading was allowed.
- `settlementType` was `real`. In this field, `real` means underlying-asset settlement, not the
  account environment; the `/demo/` URL is what makes the operation virtual.
- Minimum amount/exposure was USD 10 for both instruments.

Market-open body:

```json
{
  "action": "open",
  "transaction": "buy",
  "orderType": "mkt",
  "instrumentId": 100000,
  "settlementType": "real",
  "amount": "100.00",
  "leverage": 1,
  "orderCurrency": "usd"
}
```

For short positions, `transaction` is `sellShort`. `amount` is the invested amount; full notional
also depends on leverage. Do not substitute `direction` for `transaction` or `currency` for
`orderCurrency`.

After submission, poll the order lookup endpoint by numeric `orderId`. HTTP 404 can occur briefly
before an accepted order becomes visible; it is not permission to resubmit the mutation. A filled
result has status ID `3` and a nonempty `positionExecutions` list.

Confirm direct positions through the demo P&L endpoint. A direct position has `mirrorID: 0` and
`parentPositionID: 0`.

For a full close, send the instrument ID and omit `UnitsToDeduct`:

```json
{
  "InstrumentID": 100000
}
```

For a partial close, also supply the exact decimal `UnitsToDeduct`. Poll the returned close-order ID
through the close-order lookup endpoint. Never blindly repeat a close whose outcome is unknown.

## Verified direct-demo example

On 2026-10-07, the ordinary demo account was read through
`GET /api/v1/trading/info/demo/pnl`. Before the test it reported:

- USD 99,600 virtual cash.
- No direct positions.
- One unrelated demo copy mirror.

Two USD 100 leverage-one market buys were then submitted to the `/demo/orders` endpoint and
confirmed through numeric order lookup:

| Asset | Order ID | Position ID | Filled amount | Units | Open rate | Opened (UTC) |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| BTC | `387139953` | `3614918013` | USD 99.97 | `0.001199` | USD 83,378.45 | 2026-10-07 19:59:17 |
| ETH | `387139954` | `3614918014` | USD 100.00 | `0.038936` | USD 2,568.26 | 2026-10-07 19:59:18 |

The first BTC lookup was filled immediately. ETH lookup returned temporary HTTP 404 responses and
then status `Filled` on the third poll; the original order was not resubmitted.

The post-trade demo P&L response reported:

- USD 99,398.23 virtual cash.
- Two direct positions.
- `mirrorID: 0` and `parentPositionID: 0` on both positions.

The positions appeared in **Virtual Portfolio → Portfolio**, outside the copy sub-portfolio. No
Agent Portfolio endpoint, creator investment, copy operation, or real trading route was used.

The IDs above describe that verified run. Before modifying or closing a position later, read the
current demo P&L snapshot and confirm that the position is still open and belongs to the intended
account.

## Persistence and recovery

The Agent Portfolio service stores previews, intents, reservations, audit events, token-scope
evidence, and separate strategy/owner ledgers in dbzero. Fixed roots are:

- Demo: `/dbzero-data/trader-dev`
- Real: `/dbzero-data/trader`

Submissions use durable barriers around the trader reservation, control reservation, broker outcome,
and trader projection. An ambiguous mutation remains `UNKNOWN`; reconciliation looks it up and does
not blindly retry it. Strategy fills and owner-copy fills are independent facts.

Direct REST calls are outside `TraderService` persistence and ownership. A production direct-demo
CLI should add its own durable request, lookup, and audit records before it is used unattended.

## Native local UI API

`trader_api.ui_api` provides typed Python results for an already selected local trader.
It has no web framework dependency or UI authentication layer. Construct the existing
`TraderService` using its normal verified broker configuration, select its trader prefix,
then bind one application session:

```python
from trader_api.ui_api import (
    Period, get_dashboard, get_operation, get_statistics, list_operations,
    open_session, request_refresh, start_refresh, stop_refresh,
)

service.store.open(service.prefix)  # Select once at the application boundary.
session = open_session(service)
start_refresh(session)
try:
    dashboard = get_dashboard(session)
    statistics = get_statistics(session, Period.WEEK)
    page = list_operations(session, limit=100)
    if page.items:
        detail = get_operation(session, page.items[0].intent)
    if page.next_cursor is not None:
        next_page = list_operations(session, cursor=page.next_cursor)
    request_refresh(session)  # Coalesced; returns status without waiting for the broker.
finally:
    stop_refresh(session)  # Join the collector before closing dbzero.
```

Every subsequent function receives the captured session, never a prefix. Ambient prefix
changes do not redirect it. Cross-prefix entity handles and cursors from another session
or query are rejected. Close and reopen the session after restarting dbzero; obtain new
Python handles from the persisted records. Results contain native `Decimal`, aware UTC
`datetime`, enums and memo entity handles. Their collections are detached; use handles
only to identify entities, not as mutable result data. There are no generated UUID links.

`get_dashboard` reads lifecycle, policy budgets, owner allocation, strategy valuation,
current operation counts and refresh status. Owner allocation and the owner risk budget
are not owner equity and are never included in strategy equity. Missing broker metrics
are `None`, with completeness reasons. The eToro reader recognizes explicit USD `equity`,
`cash`/`availableCash`/`credit`, `unrealizedPnl`/`unrealizedPnL` and `exposure` fields in the
flat identity-bearing response or `clientPortfolio` envelope. Credit alone does not imply
equity. Unknown shapes or responses with no recognized metrics report `VALUATION_UNAVAILABLE`.
Custom brokers can implement the `PortfolioReader` protocol in `broker.observations`.

`get_statistics` selects UTC today, Monday week-to-date, month-to-date or collection
lifetime (`Period.TODAY`, `WEEK`, `MONTH`, `ALL`). Summaries are persisted during updates.
Reads never scan historical observations or create a new period. Before the next update
after rollover, that period reports `period_not_collected`. Confirmed costs and realized
P&L come from cumulative per-operation broker reports, with only changes from the prior
checkpoint applied. A correction is recognized in the period when it is received, without
rewriting closed periods. Estimates and opening-notional ledger entries never enter
confirmed P&L. Missing reports remain explicit, including when only part of a total is known.
Operation-state counts are the last persisted current-state distribution, not a count of
transitions. Equity change and absolute maximum drawdown describe sampled strategy equity;
they are not cash-flow-adjusted returns. Unknown intervals and mid-period collection starts
make periods incomplete. There is no historical backfill or old-schema migration workflow.

`list_operations`, `list_positions`, `list_orders`, `list_audit_events`, and
`list_accounting_observations` use indexed filters and ascending sequence pagination.
Default size is 100; the maximum is 1,000. An operation detail contains first pages of its
related records; continue through the corresponding list function using the same intent
filter. A cursor fixes the append ceiling so concurrent inserts do not grow an ongoing
scan. State filters use the state at each page read: records that change state may enter
or leave the remaining pages. Audit filters include kind, actor, source, intent and time;
all time ranges are start-inclusive and end-exclusive. Audit hashes and source facts are
unchanged by the UI API.

`get_portfolio_chart(session, start, end, resolution=None)` reads stored minute, hour or
day buckets in UTC time order. Automatic resolution picks the finest range that fits
1,000 buckets; longer-than-daily-cap ranges are rejected rather than truncated. Boundary
buckets contain the whole stored interval, and absent buckets remain gaps. Each bucket
contains first/last/minimum/maximum equity, latest supported components, sample count
and completeness. Neither chart nor statistics reads regroup source history.

Refresh starts immediately, then runs every 60 seconds. Importing the package starts no
threads. At most one collector owns a trader prefix in the process; repeated starts are
idempotent for its session. Manual requests during an in-flight collection are satisfied
by that collection; queued requests respect backoff. `get_refresh_status` reports running,
refreshing and queued states, attempt/success times, next attempt, sanitized error,
generation and staleness (no success for over 120 seconds). Failures preserve last-good
data and retry after 60, 120, 240, then 300 seconds, or later when `Retry-After` requires it.
Persisted retry deadlines survive restart; interrupted attempts never become successes.
The worker rechecks the retained credential evidence and portfolio binding before and
after network collection. Refresh never dispatches trades or runs trading reconciliation.

Application database transactions, execution projections and UI updates share a lock;
network collection runs outside it. Source observations, checkpoints and affected
summaries/buckets commit atomically. Direct application mutations of memo objects should
use `service.store.transaction(service.prefix)` and the write-side update hooks; bypassing
those hooks cannot maintain the UI statistics. Public function docstrings specify all
arguments, ordering, freshness, scope, side effects and error behavior.

## Common errors

| Code | Meaning |
| --- | --- |
| `ENVIRONMENT_UNVERIFIED` | No verified scope record exists for the selected child token. |
| `ENDPOINT_ENVIRONMENT_MISMATCH` | Configured trading URLs disagree with verified scopes. |
| `ENVIRONMENT_ASSERTION_MISMATCH` | `--expected-environment` disagrees with verified scopes. |
| `PORTFOLIO_NOT_BOUND` / `PORTFOLIO_NOT_READY` | Owner provisioning/binding is missing or incomplete. |
| `COPY_DIVERGENCE` | New Agent Portfolio exposure is paused pending actual mirror reconciliation. |
| `STALE_PREVIEW` | Preview expired or broker/binding/policy state changed. |
| `IDEMPOTENCY_CONFLICT` | A key was reused for another preview. |
| `BROKER_OUTCOME_UNKNOWN` | Mutation may have reached the broker; reconcile rather than retry. |
| `PERMISSION_REVOKED` | The broker rejected or revoked the credential. |

## Development checks

```bash
.venv/bin/ruff check .
.venv/bin/mypy trader_api
.venv/bin/pytest -m 'not external_demo'
.venv/bin/pytest -m external_demo
.venv/bin/pytest --cov=trader_api --cov-report=term-missing
graphify update .
```

External tests must use demo trading routes and dedicated data. A blocked broker prerequisite is a
blocked test, not a pass. Never run automated trading tests against real-money endpoints.

## Official eToro references

- [Authentication and Demo/Real API keys](https://api-portal.etoro.com/core/getting-started/authentication)
- [Create Agent Portfolio v2](https://api-portal.etoro.com/api-reference/agent-portfolios/create-agent-portfolio-v2)
- [Demo market-order guide](https://api-portal.etoro.com/core/guides/market-orders)
- [Demo copy trading](https://api-portal.etoro.com/api-reference/copy-trading--demo/start-copying-an-investor-or-adjust-an-existing-copy)
- [Get demo portfolio and P&L](https://api-portal.etoro.com/api-reference/trading--demo/get-demo-account-pnl-and-portfolio-details)
