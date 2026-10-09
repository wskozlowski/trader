# NiceGUI trader workspace

## Status and purpose

This document specifies a browser UI for the existing trader service. It is an
implementation specification, not an implementation of the application. The UI adds no
new trading semantics: it selects already provisioned traders, starts their local runtime
and portfolio collection, presents the native `ui_api` projections, and exposes the manual
operations already supported by `TraderService`.

In this document, **open a trader** or **start a trader** means:

1. construct and verify that trader's existing `TraderService` from a server-side catalog;
2. capture a trader-scoped native `ui_api.Session`; and
3. start that session's observational portfolio collector.

It does not activate an uninitialized portfolio. Activation remains a separate, explicit
user action. Owner provisioning, allocation changes, credential management, retirement,
and automated strategy execution are outside this UI.

One server process serves exactly one verified environment. There is deliberately no UI
authentication. The process therefore belongs only on a trusted host or behind a deployment
boundary that supplies access control; binding to `127.0.0.1` is the safe default. The UI
must not suggest that a trader catalog key is an authentication credential.

## Goals and non-goals

The implementation must provide:

- selection of configured, existing traders without accepting paths or database identifiers
  from the browser;
- independent navigation, forms, filters, pagination, and preview state in each browser tab;
- one shared runtime and collector when multiple tabs open the same trader;
- overview, portfolio-insight, position, order, operation, and audit views backed by the
  existing native `trader_api.ui_api` reads;
- explicit activation for an existing `READY`, uninitialized binding;
- preview-and-submit workflows for the existing open, cancel, modify-protection, and
  partial/full-close operations; and
- explicit execution reconciliation, separate from observational portfolio refresh.

The implementation must not provide:

- browser login, user or role management;
- owner provisioning, copy-allocation changes, token or credential management;
- arbitrary profile paths, dbzero prefixes, database UUIDs, or broker-account selection;
- portfolio retirement or automated strategy scheduling;
- direct browser access to `TraderService`, dbzero, broker clients, memo objects, or cursors;
- reconstruction of accounting totals from currently displayed rows; or
- automatic retries of financially ambiguous commands.

## Dependency, executable, and process model

NiceGUI is an optional presentation dependency. Add the following optional dependency and
entry point when implementing this design:

```toml
[project.optional-dependencies]
ui = ["nicegui==2.24.2"]

[project.scripts]
trader-ui = "trader_api.web_ui.main:main"
```

Version `2.24.2` is intentional. It is the version with which the selected SellTime
`ui.sub_pages`, table, chart, and persistent-shell patterns have been verified. Core service
modules and `trader_api.ui_api` must remain importable and testable without NiceGUI installed.

The supported invocation is:

```bash
trader-ui --catalog trader_ui.toml --environment demo --host 127.0.0.1 --port 8080
```

`--environment` is required and is an equality assertion against verified profile scopes; it
does not choose or rewrite a credential environment. Accept only `demo` or `real`. The process
must run with NiceGUI reload disabled and one server worker. Do not expose a worker-count
option. Multiple processes would each create registries and collectors while attempting to
use dbzero's process-global root, so they are unsupported.

`main()` parses configuration, validates the complete catalog, creates the runtime registry,
registers startup and shutdown hooks, builds the NiceGUI routes, and finally calls `ui.run`
with `reload=False`. Importing a web module must not start NiceGUI, a collector, or a broker
call.

## Package layout and dependency direction

All framework-specific code belongs under `trader_api/web_ui/`:

```text
trader_api/
  ui_api/
    api.py                 # existing native reads
    commands.py            # new synchronous, framework-independent command facade
    types.py               # existing reads plus new typed command results
  web_ui/
    __init__.py
    main.py                # CLI entry, hooks, ui.run
    catalog.py             # TOML parsing and immutable CatalogEntry values
    runtime.py             # process registry, runtime lifecycle, executors, broker gate
    controllers/
      workspace.py         # one tab/client's state and route epochs
      reads.py             # async offload, refresh policy, stale-result rejection
      commands.py          # form -> typed request -> preview -> submission
    presentation.py        # browser-safe rows, cards, enums, and view-local row keys
    formatting.py          # Decimal/time/status formatting only
    theme.py               # neutral light theme and responsive financial-table CSS
    pages/
      select_trader.py
      overview.py
      portfolio.py
      positions.py
      orders.py
      operations.py
      audit.py
    components/
      shell.py
      status_strip.py
      metric_card.py
      paged_table.py
      state_panel.py
      operation_drawer.py
      command_dialog.py
```

The exact split may combine very small files, but the dependency direction is mandatory:

```text
NiceGUI pages/components -> controllers -> ui_api -> TraderService/dbzero/broker
```

Neither core trading code nor `ui_api` may import NiceGUI. Pages build presentation and bind
callbacks; they do not query dbzero, convert service identifiers, call the broker, or contain
command policy. Reusable components receive immutable presentation values and callbacks, as
in SellTime's verified component pattern.

## Server-side trader catalog

### Format

Use `tomllib` to load one explicit server-side file. A minimal catalog is:

```toml
version = 1
environment = "demo"

[traders.alpha]
label = "Alpha strategy"
trader = "alpha"
profile = ".env_demo_alpha"

[traders.income]
label = "Income strategy"
trader = "income"
profile = ".env_demo_income"
```

The table name (`alpha`, `income`) is the entry's **public catalog key**. It is a routing
identifier, not cryptographic key material. A `CatalogEntry` contains only:

```python
@dataclass(frozen=True, slots=True)
class CatalogEntry:
    key: str
    label: str
    trader_name: str
    profile_filename: str
```

Credentials remain in the existing profile files. The catalog must not contain API keys,
user keys, service credentials, dbzero roots, prefixes, or arbitrary broker URLs.

### Validation

Validate the entire catalog before opening the listener:

- `version` must be exactly `1` and `environment` must equal the command-line assertion.
- Keys must match `[a-z][a-z0-9_-]{0,63}`. Labels and trader names must be non-empty and
  length-bounded.
- `profile` must be a filename, not an absolute or multi-component path. The existing
  `load_profile` check remains authoritative for project-root containment and file mode.
- TOML already rejects duplicate table keys. Additionally reject repeated trader names and
  repeated normalized `(trader, profile)` entries. The registry must never have two public
  keys for one trader runtime.
- Reject unknown top-level keys and unknown entry fields so misspellings do not silently
  select defaults.

Structural validation does not prove an environment. When an entry is first opened,
construct `TraderService(entry.trader_name, entry.profile_filename,
expected_environment=process_environment)`. Its verified scope and binding checks are
authoritative. A wrong-environment, mixed-route, invalid-profile, or unverified entry enters
`FAILED_STARTUP`; no session or collector is created. The trader-selection card shows only a
sanitized `TraderError.code` and safe guidance, never an exception string, profile contents,
credential fingerprint, or path.

The browser may send only a catalog key. It must never submit `trader_name`, `profile`, a
filesystem path, environment, storage key, prefix, or database identifier. The server always
resolves the current immutable `CatalogEntry` again.

## Shared runtimes and independent workspaces

### Process-wide runtime registry

`RuntimeRegistry` is process-wide and keyed by public catalog key. It owns the process
environment and has these states:

```text
NOT_STARTED -> STARTING -> RUNNING -> STOPPING -> STOPPED
                    `----> FAILED_STARTUP
```

The registry uses a lock plus a per-key start future so simultaneous first opens create
exactly one runtime. A successful `TraderRuntime` owns exactly:

- one verified `TraderService`;
- one captured native `ui_api.Session`;
- that session's one `RefreshWorker`;
- one `ThreadPoolExecutor(max_workers=1)` for activation, preview, submit, and reconciliation;
- one per-trader broker gate shared by command work and the collector; and
- lifecycle state, accepting-commands flag, and sanitized startup failure if applicable.

The runtime is lazy: selecting or navigating to a catalog entry starts it. It is not
reference-counted to tabs. Once started, it continues collecting even if every tab navigates
away, and stops only during application shutdown. A retry after `FAILED_STARTUP` is not
automatic; a deliberate server-side retry action may be added later, but ordinary browser
refresh must not repeatedly load credentials or hammer broker verification.

`start_refresh(session)` runs only after `open_session(service)` succeeds. The existing
refresh registry is a second invariant check, not the primary runtime registry. Any
`REFRESH_RUNNING` error is treated as an implementation/lifecycle fault and exposed only as a
safe failed-startup code.

### Per-tab workspace

Each NiceGUI client connection (normally one browser tab) gets one server-held
`WorkspaceState`. It contains:

- a random workspace identity and monotonically increasing route epoch;
- the selected catalog key and a reference to the shared runtime;
- per-view filters, page size, current cursor stack, selected row, and drawer state;
- form text, draft revision, prepared preview, and submission display state;
- last displayed generation and UTC period boundary; and
- in-flight read tickets and client-owned timers.

Do not store this state in process globals, URL query values, or browser local storage.
NiceGUI's client-scoped server state may be used, but the authoritative objects and memo
handles stay in the server process. Navigating one tab to another trader updates only that
tab's workspace. A second tab has a different workspace even when it shares the same runtime.

Changing traders invalidates the old route epoch, clears server-held row/preview maps from the
active view, and creates or attaches to the new runtime. It does not stop either runtime.
Optionally retain non-sensitive view preferences keyed by `(workspace, catalog key, view)` so
back-navigation can restore filters, but never carry entity handles or a prepared preview
between traders.

### Callback and result validity

Every asynchronous load captures a `LoadTicket` containing workspace identity, catalog key,
runtime epoch, route epoch, view incarnation, and request sequence. Before touching a UI
element, its completion callback verifies all fields still match and that the client remains
connected. Otherwise it discards the result. This prevents late responses from an old trader
or page from repainting the current workspace.

Every mutation callback resolves its prepared command or row handle in the active workspace
and checks the same ownership fields. Foreign handles, stale row keys, disposed views, and
callbacks from another workspace fail closed with `INVALID_HANDLE` or a presentation-level
"selection expired" message. They never fall back to a browser-supplied broker ID.

## Navigation and persistent shell

Follow SellTime's verified NiceGUI pattern: render one persistent header and left sidebar,
then render page content through `ui.sub_pages`. Page builders are separate functions and
receive route parameters by name. The route map is:

```text
/                                      trader selection
/traders/{trader_key}/overview         overview
/traders/{trader_key}/portfolio        portfolio insights
/traders/{trader_key}/positions        positions
/traders/{trader_key}/orders           orders
/traders/{trader_key}/operations       operations
/traders/{trader_key}/audit            audit
```

There are no entity-detail URLs. Position, order, operation, and audit selection uses a
view-local row key and a drawer/dialog owned by that view. A route contains only the public
catalog key. Unknown or unavailable catalog keys return to selection with a safe message.

The persistent header shows the product name, selected trader label, asserted/verified
environment, lifecycle, and freshness. The sidebar contains the views above and a trader
switcher. The trader/environment/freshness indicators remain visible on narrow screens; the
sidebar may collapse into a drawer. Navigation highlights the current page and uses
`ui.navigate.to` so `ui.sub_pages` replaces only content without rebuilding the connection and
shell.

Use English labels, a neutral light theme, tabular numerals, accessible status colors with
text/icon redundancy, and dense financial tables. At mobile widths cards stack and tables
scroll horizontally; do not hide identifying columns or action state.

## Browser-safe presentation boundary

Native `ui_api` return values remain server-side. Controllers convert them to immutable
presentation models containing strings, booleans, enums, datetimes formatted for display,
and view-local random row keys. The following never enters NiceGUI props, websocket payloads,
URLs, DOM attributes, logs, or browser storage:

- dbzero memo objects and proxies;
- `db0.uuid(...)` values, storage keys, prefixes, and control identifiers;
- `ui_api.Cursor` objects or their fields;
- profile filenames, credentials, credential fingerprints, and raw broker payloads; and
- server-held idempotency keys.

External broker order and position numbers may be shown as informational text where useful,
because they are already part of the supported trading projection, but callbacks must still
resolve the server-held row handle. A typed `RowHandleMap` is scoped to one workspace, trader,
view incarnation, and current result set. Clearing or replacing a table clears its old map.

Pagination is cursor-based even though `ui.table` presents page controls. For each filter
snapshot, the controller stores the cursor used to load each visited page and the resulting
next cursor. "Previous" uses the stored earlier cursor; changing filters or page size resets
the stack. Cursors never cross sessions, filters, or workspaces. Bounded pages use a default
of 100 and allow only configured choices no greater than the native API maximum of 1,000.

## Formatting and numeric rules

`Decimal` is mandatory for money, rates, fractions, quantities, budget arithmetic, and exact
review labels. Formatting functions accept `Decimal | None` and return text. `None` displays
as an em dash plus an accessible "Unavailable" label or tooltip; it is never formatted as
zero. Reasons supplied by `ui_api` explain why a value is missing or incomplete.

Form fields hold raw text. Only the preview action parses text into finite `Decimal` values and
constructs a typed request. Do not bind financial fields to JavaScript numbers or Python
`float`. Preserve the text after validation so review and edit round-trips do not change its
meaning.

Conversion to `float` is allowed only while building ECharts coordinate arrays. Tooltip and
axis-detail formatters must use separately retained exact formatted labels when displaying a
value. No `float` derived from the chart can flow back into budgets, statistics, preview
requests, or command submission.

Always label strategy equity, owner allocation, strategy policy budget, and owner-mirror
policy budget separately. Owner allocation is not strategy equity, and neither budget is a
broker guarantee. Never calculate dashboard totals by summing the currently displayed
position page.

## Read models and pages

All dashboard, statistics, chart, and list data comes from the existing native
`trader_api.ui_api`. These functions return detached values from the last committed local
generation and perform no network work. Controllers must not replace them with calls to the
CLI's JSON-shaped `positions()`, `orders()`, `portfolio_history()`, or similar compatibility
methods.

### Trader selection

Render one card per catalog entry with its label and runtime state: not started, starting,
running, failed startup, stopping, or stopped. A running card also shows lifecycle,
initialized/read-only state, freshness, and its last safe refresh error. The primary action is
**Open**.

Opening starts or reuses the runtime and navigates to overview. If the binding is `READY` but
uninitialized, overview presents the activation panel before trading views. Starting the
runtime still starts collection; activation never occurs implicitly. Failed startup cards
show the sanitized error code and operational guidance to correct the server-side profile and
restart.

### Overview

Read `get_dashboard(session)` and the desired persisted statistics, never broker endpoints.
Show:

- strategy equity, cash, unrealized P&L, and exposure from `Dashboard.valuation`;
- confirmed realized P&L and confirmed costs from persisted `Statistics` (lifetime by
  default, with the period explicitly labeled);
- owner allocation as a separate metric;
- strategy and owner budgets, each showing initial cap, realized component, committed amount,
  and available-to-open amount;
- operation counts by execution state; and
- collector running/refreshing/queued state, last attempt, last success, next attempt,
  generation, stale state, and sanitized error.

Missing valuation or accounting metrics show unavailable/incomplete explanations. Do not
combine confirmed realized P&L with unrealized P&L into an invented return number.

Provide **Refresh portfolio** as `request_refresh(session)`. It queues observational
collection only and is labeled accordingly. It does not reconcile command execution.

### Portfolio insights

Read `get_statistics` independently for today, UTC week (Monday), UTC month, and collection
lifetime. Show collection start, first/last observation, latest metrics, sampled absolute
equity change, sampled peak, maximum drawdown, sample count, missing intervals, confirmed
realized P&L/costs, operation counts, freshness, and each completeness reason.

The equity chart uses `ui.echart` with data from `get_portfolio_chart`. Its initial range is
`[now - 24 hours, now)` and passes `resolution=None` so the API chooses the finest stored
resolution within its 1,000 bucket limit. Range controls may request minute/hour/day
explicitly, but must explain and surface `INVALID_RANGE`; they must not fetch raw observations
or regroup buckets in the UI.

Build one aligned coordinate per expected bucket in the returned range. For a missing bucket,
insert `null`; for a stored bucket with unavailable equity, also use `null` and retain its
reasons. Configure ECharts not to connect nulls, so collection gaps remain visible. Mark the
currently incomplete boundary bucket, show first/last observation and sample count in its
tooltip, and display `missing_intervals`, `reasons`, stale status, resolution, and generation
near the chart. Never infer a zero value or interpolate a missing interval.

### Positions

Use `list_positions` with its indexed `state`, `symbol`, and `intent` filters and keyset
pagination. The main table shows symbol, side, state, notional, units, leverage, stop loss,
take profit, creation time, and informational broker position number. A detail drawer may
load the originating operation through its server-held intent handle.

For an eligible open position, the drawer provides:

- **Change protection**, supporting stop-loss rate, take-profit rate, and the service's
  supported stop-loss types (`rate` and `trailing`); and
- **Close position**, accepting a fraction in `(0, 1]` with explicit shortcuts for full close
  and common partial fractions.

The fraction stays a decimal string until preview validation. A full close is fraction `1`;
it is not a distinct backend operation. Editing or action selection never uses a position ID
typed by the browser.

### Orders

Use `list_orders` for both pending and historical local projections. Provide indexed state,
symbol, and related-intent filters with bounded cursor pagination. Visually distinguish
`PENDING`/`ACKNOWLEDGED` from terminal projections and state clearly that these are local
committed projections, not an implicit live broker lookup.

Eligible pending opening orders offer **Cancel order**. A related-operation action opens the
operation drawer using the server-held intent handle; it does not construct an intent UUID
link. If cancellation races with a fill, show the durable returned/unknown state and direct
the user to **Check executions** rather than assuming cancellation.

### Operations

Use `list_operations` with operation, execution state, and UTC start/end filters. The table
shows sequence, time, operation, state, safe normalized parameters, error code, and
informational broker references where present.

The detail drawer calls `get_operation` for related orders, positions, and accounting, and
`list_audit_events(intent=...)` for audit events. Each nested collection respects its own
cursor if it exceeds the first page. Display estimated versus confirmed accounting distinctly;
an unconfirmed estimate never contributes to confirmed statistics. Detached facts may be
rendered as a safe key/value table after presentation conversion, never as raw `repr`.

### Audit

Use `list_audit_events` with the native indexed UTC time, kind, actor, source, and intent
filters. Preserve ascending immutable sequence and the API's frozen append ceiling. Display
facts only after native detachment and browser-safe conversion. Display `previous_hash` and
`event_hash` unchanged in a copyable monospaced field; do not shorten, recompute, or "verify"
them in the browser. Chain verification, if later exposed, must call the backend verifier and
be separately labeled.

## Refresh, generation, and page preservation

Each active workspace owns a NiceGUI timer firing every two seconds. Its callback performs a
local `get_refresh_status(session)` read off the event loop. It does not request a broker
refresh. Dispose of the timer when the workspace changes trader, the page/client is disposed,
or the socket disconnects.

Reload visible data when any of these occurs:

- the committed generation changes;
- the current UTC day/week/month boundary relevant to the visible statistics rolls over;
- the user explicitly navigates/reloads the view; or
- the current command completes and its projection may have changed.

Generation changes are hints to reload persisted data, not authorization to reset user state.
Preserve filters, form text, selected period, drawer context, and pagination. A table may be
reloaded in place only if doing so preserves the inspected slice. If changed state membership
or a reset cursor could replace/reorder the current rows, show **New data available** and wait
for the user to reload the table. Overview metric cards and an unopened first table page may
refresh immediately.

A rollover reload may legitimately return `period_not_collected` until the collector commits
the new persisted period. Show the incomplete state; do not create a period, backfill, or copy
yesterday's values. Coalesce timer ticks while a status/read call is already pending. Late
results are subject to the `LoadTicket` checks described above.

## Framework-independent command API additions

### Boundary

Add a synchronous native facade in `trader_api/ui_api/commands.py` and export it from
`trader_api/ui_api/__init__.py`. It delegates to the existing `TraderService`; it does not
duplicate order mapping, admission, lifecycle, idempotency, or outcome projection. Existing
public `TraderService` methods and CLI arguments/results remain compatible.

The facade accepts captured `Session` objects and native memo handles obtained through the
same session. It validates handles with the existing scope checks, converts a position/order/
preview handle to the service's current identifier only inside this backend boundary, invokes
the service, and converts its result back to typed native values. Pages and controllers never
call `db0.uuid`, `_api_reference`, `_owned_position`, or `_owned_order`.

The following names illustrate the required contract; implementation may refine field names
but not weaken typing or scope checks:

```python
class OpeningOrderType(StrEnum):
    MARKET = "market"
    MARKET_IF_TOUCHED = "market_if_touched"
    LIMIT_IOC = "limit_ioc"

@dataclass(frozen=True, slots=True)
class OpenOrderRequest:
    symbol: str | None
    instrument_id: int | None
    side: Side
    order_type: OpeningOrderType
    strategy_notional_usd: Decimal
    leverage: int
    trigger_rate: Decimal | None = None
    limit_rate: Decimal | None = None
    stop_loss_rate: Decimal | None = None
    take_profit_rate: Decimal | None = None

@dataclass(frozen=True, slots=True)
class CancelOrderRequest:
    order: Order                         # server-held, same-session handle

@dataclass(frozen=True, slots=True)
class ModifyPositionRequest:
    position: Position                   # server-held, same-session handle
    stop_loss_rate: Decimal | None
    take_profit_rate: Decimal | None
    stop_loss_type: str | None

@dataclass(frozen=True, slots=True)
class ClosePositionRequest:
    position: Position                   # server-held, same-session handle
    fraction: Decimal

@dataclass(frozen=True, slots=True)
class ActivationRequest:
    expected_investment_usd: Decimal
    currency: Currency = Currency.USD
```

`OpenOrderRequest` requires exactly one of symbol/instrument ID. Order-specific required and
forbidden rates, finite positive notional, leverage, protection, capability, and eligibility
rules remain authoritative in `TraderService`/the broker adapter. The facade performs only
obvious type/shape checks early and does not create a second policy engine.

Preview functions are:

```python
prepare_open(session, request) -> PreparedCommand
prepare_cancel(session, request) -> PreparedCommand
prepare_modify(session, request) -> PreparedCommand
prepare_close(session, request) -> PreparedCommand
submit_prepared(session, prepared) -> CommandResult
activate(session, request) -> ActivationResult
check_executions(session) -> ReconciliationResult
```

`PreparedCommand` is scope-bound and non-serializable. It privately holds the persistent
`Preview` memo handle and exactly one randomly generated idempotency key created when the
preview succeeds. Its public typed review fields include:

- operation, creation and expiration time;
- affected entity summary;
- exact requested notional, fraction, units, leverage, rates, and protections as applicable;
- estimated strategy and owner copied notional/costs when available;
- strategy and owner reservation and remaining-budget effects; and
- completeness/capability notes.

Do not expose `broker_payload`, the broker-state fingerprint, memo handle, preview UUID, or
idempotency key to the browser. The facade extracts only safe review fields from the service's
existing preview result. The web controller composes those values with the catalog label/key
and the runtime's verified environment so every review still identifies its target; catalog
metadata is not part of the native trading request. `submit_prepared` verifies the prepared
command belongs to the same session and has not been substituted, converts its preview handle
internally, and calls `TraderService.submit` with its stored key. Two callbacks holding the
same prepared command therefore use the same key and receive the same durable intent. Reusing
a key for another preview remains an `IDEMPOTENCY_CONFLICT` at the service boundary.

`CommandResult` privately holds the native `Intent` handle and publicly reports operation,
execution state, safe error code, times, affected entity summary, and broker reference text if
available. It must preserve at least these distinct presentation outcomes:

- **filled/completed**: a confirmed terminal result;
- **pending acknowledgement**: committed, admitted, acknowledged, or pending work that may
  still change;
- **rejected**: broker or authoritative validation rejection with no implication of execution;
- **unknown**: the broker outcome is ambiguous and reservation/recovery state remains; and
- **canceled**: a confirmed terminal cancellation.

`ReconciliationResult` types `recovered_intents`, `unresolved_intents`, and the existing owner
mirror requirement. Execution reconciliation concerns strategy command outcomes. It does not
claim the owner mirror is reconciled; always retain the visible
`requires_owner_reconciliation` message when returned.

Expose a typed `get_command_availability(session) -> CommandAvailability` beside these
functions. It translates, but does not replace, the service's verified lifecycle, initialized
state, read/write scopes, copy health, and effective broker capabilities. It reports separate
booleans plus safe reason codes for activation, opening, close, cancel, protection, and
execution reconciliation. This prevents pages from interpreting the current untyped
`capabilities()` dictionary independently. Entity-specific checks still happen when the
handle is resolved and again in `TraderService`.

### Activation

For a verified `READY`, uninitialized binding, show the approved owner allocation, strategy
virtual balance, currency, and copy-health status. Activation requires a deliberate dialog and
an `ActivationRequest` whose expected investment matches the displayed existing allocation.
Call the typed `activate` facade, which delegates to `TraderService.initialize`. It must never
provision, invest, change the allocation, create credentials, or broaden scopes. An identical
retry retains the service's current idempotent behavior.

### Edit, preview, review, submit, outcome

Every mutation uses this state machine:

```text
EDITING --Preview--> PREVIEWING --> REVIEW
   ^                    | error       | input change / expiry
   |                    `-------------+-----------------------> EDITING
   |                                      |
   |                                   Submit
   |                                      v
   `-- new action <--- OUTCOME <--- SUBMITTING
```

Preview is broker-backed and may resolve sizing, costs, eligibility, or state fingerprints. It
therefore runs through the runtime command executor and broker gate, never in the NiceGUI event
loop. The review dialog displays trader, environment, affected entity, exact quantities,
estimated costs, both budget effects, creation time, and expiry.

Each input event increments a draft revision and immediately invalidates/removes the prepared
command. A late preview result is accepted only if its captured draft revision and workspace
ticket still match. Expiry disables submit and returns the form to editing without silently
creating a replacement preview.

On submit, disable preview/submit controls and enqueue exactly the prepared command. Defensive
double-click callbacks reuse the same object and key. UI disabling is usability protection;
service idempotency is the correctness protection. Navigation or disconnection may detach the
display callback but must not cancel a command once it has been submitted to the executor.
After broker dispatch begins, cancellation is never attempted. The server persists and
projects the outcome even if no client remains.

Treat errors precisely:

- `STALE_PREVIEW` means broker state, policy, binding, or time changed; return to edit and
  require a new preview.
- Authoritative input/admission errors mean **not submitted**; retain the draft and show the
  safe code/guidance.
- `REJECTED` is a known negative durable outcome; do not describe it as unknown.
- `ACKNOWLEDGED`/pending means the broker accepted or reported pending work; keep its operation
  visible and offer execution checking where appropriate.
- `UNKNOWN` means execution may have happened. Never automatically call submit again, create a
  new preview as a retry, or release risk based on a lookup miss.

Provide a clearly separate **Check executions** action that calls `check_executions` through
the command executor. Ordinary portfolio refresh and the two-second status poll must never
call reconciliation. After reconciliation completes, reload affected local views or show the
new-data indicator. The returned owner-mirror reconciliation requirement stays visible.

## Action availability and lifecycle

The UI computes display availability from a typed backend capability result, but backend
validation at preview, submit, activation, and reconciliation remains authoritative. Never
enable an action solely because the page has an entity row.

Opening new exposure and risk-reducing/control operations are separate capability classes:

| Condition | Open exposure | Close | Cancel | Protection | Activate | Reads |
| --- | --- | --- | --- | --- | --- | --- |
| Verified `ACTIVE`, initialized, read/write, copy healthy | If broker capability allows | Yes | Yes | Yes | No | Yes |
| Copy divergence | No | Yes | Yes | Yes | No | Yes; show owner action required |
| Read-only scope | No | No | No | No | No | Yes; persistent read-only banner |
| `READY`, uninitialized | No | No | No | No | Explicit only when eligible | Yes |
| `SUSPENDED`, initialized and write scope | No | Backend-permitted risk reduction | Backend-permitted | Backend-permitted | No | Yes |
| `RETIRED` | No | No | No | No | No | Preserved local history only |
| Failed startup/disconnected/stopping | No | No | No | No | No | No live reads |

Entity state and broker capabilities can further disable a cell. For example, cancel applies
only to a service-owned pending/acknowledged order, and close/modify applies only to a
service-owned open position. Copy divergence must not be implemented as a blanket disable of
close, cancel, or supported protection changes.

Review the current `TraderService.reconcile` lifecycle check while adding the typed facade:
unknown execution outcomes may still require safe read-only reconciliation when new exposure
is blocked. Any lifecycle broadening must be explicit, tested, and preserve the CLI's existing
active-runtime behavior; the UI must not bypass `_validate_binding`.

## Concurrency and event-loop isolation

### Offloading

No synchronous service or native API call runs on NiceGUI's asyncio event-loop thread. Local
reads use a bounded I/O executor (or NiceGUI's equivalent `run.io_bound`) and still obey the
dbzero critical section. Activation, every preview, submit, and reconciliation use the
runtime's single-thread command executor. Await executor futures asynchronously; never call
`future.result()` on the event loop.

The command executor serializes mutations and their admission checks per trader. It does not
serialize different traders' broker calls. The per-trader broker gate is also acquired by the
collector, so a collector and command cannot use the same `BrokerAdapter` concurrently.
Collectors for different traders may perform network I/O concurrently.

### Database and broker critical sections

dbzero's active prefix and physical root are process-global in the current integration. Every
dbzero read, mutation, prefix selection, and commit must occur inside a short explicitly scoped
critical section using the existing `_runtime_lock`/`selected` conventions. Never hold that
lock while waiting for an executor, acquiring the broker gate, or doing broker/network I/O.

For an operation needing both persisted and broker state, use phases:

1. under the database lock, validate scope and copy immutable scalar inputs;
2. release the database lock;
3. acquire the trader's broker gate and perform network work;
4. release the broker gate; and
5. under the database lock, revalidate required versions and atomically persist/project the
   result.

If a command requires the broker gate over multiple existing service calls, refactor the
service into explicit phases rather than holding a database lock around the combined call.
Never retain a live memo for unguarded mutation between phases; carry identifiers/scalars and
resolve/revalidate under the lock. The durable submission saga and its commit barriers remain
unchanged.

Use one lock order everywhere: registry lifecycle, runtime lifecycle, broker gate, then a
short database section only after broker I/O has finished. Code must not acquire the broker
gate while holding `_runtime_lock`. The existing per-prefix `_submission_lock` protects
idempotent submissions but is not a substitute for database serialization or the broker gate.

### Required hardening before concurrent workspaces

The current native read API already enters `selected(session)`, and
`RefreshWorker._collect` deliberately performs `collect_portfolio` outside its database
sections. Concurrent UI work must not be enabled until these remaining service sections are
audited and corrected:

- `TraderService._objects`, `_validate_binding`, `capabilities`, `trader_status`, and other
  compatibility reads currently reach the store without a consistently enclosing runtime
  lock.
- `_owned_position`, `_owned_order`, `_api_reference`, and `_persist_preview` select/query or
  commit dbzero state without one explicit short transaction covering the operation.
- `preview_open`, `preview_close`, `preview_modify`, and `preview_cancel` combine service/store
  reads with broker sizing, cost, and fingerprint calls. Split their database and network
  phases and place broker work behind the shared gate.
- `initialize` verifies broker identity and then commits activation. Keep the network call
  outside the database lock and revalidate binding/copy/policy facts before the commit.
- `submit` correctly avoids wrapping broker dispatch in `store.transaction`, but its preview
  resolution, control-prefix writes, ownership claims, direct commits, and projection steps
  still need complete database-lock coverage and explicit revalidation between phases.
- `reconcile` interleaves unresolved-control/local-intent queries, broker lookups, direct
  control commits, projection, and final broker snapshot. Restructure it into bounded read,
  network, and write phases; do not hold the dbzero lock across any lookup or snapshot call.
- Compatibility list/history/audit methods used by the CLI must remain safe after the same
  changes. Do not fix only the new UI facade while leaving shared service methods racy.
- `RefreshWorker._collect` must acquire the runtime's broker gate for its observational broker
  call while preserving its current no-network-under-`selected` property. Scope validation and
  valuation recording remain separate short database sections.
- Control/trader prefix switching in `DbzeroStore.reserve_control`, ownership claims, and all
  explicit commits must be included in the audit. No prefix may remain implicitly selected for
  a later caller.

Add concurrency tests before claiming this gate complete. The fact that existing sequential
tests pass is not evidence that two NiceGUI workspaces are safe.

## Shutdown and failure behavior

Register one application shutdown coordinator and make it idempotent. Shutdown order is:

1. mark the registry as stopping and reject new runtime starts and commands;
2. disable new collector scheduling while allowing currently running broker work to finish;
3. drain every already submitted command future, including commands whose browser disconnected;
4. stop and join every refresh worker;
5. close each broker adapter/client if it exposes a close operation;
6. shut down command/read executors; and
7. call `close_dbzero()` exactly once after all database users have stopped.

Do not cancel an in-flight broker dispatch during graceful shutdown. A bounded deployment
grace period may report that shutdown is still waiting, but must not pretend an ambiguous
command did not execute. Crash recovery remains the existing durable intent/control saga plus
explicit reconciliation.

Starting, failed, or stopping runtimes render stable states rather than uncaught exceptions.
A collector error retains the last good generation and presents stale data with its sanitized
refresh code. A failure in one trader runtime does not stop another trader runtime or switch a
tab's selected trader.

## Required UI states

Every page/component must deliberately render the following states:

| State | Presentation | Controls that remain available |
| --- | --- | --- |
| Loading | Skeleton/spinner with page title and persistent status strip | Navigation; no entity actions |
| Empty | Specific explanation such as "No local orders match these filters" | Filters, navigation, valid create/open action |
| Stale | Last-success time and warning; retain last good values | Local reads, explicit portfolio refresh, lifecycle-allowed manual actions after fresh broker-backed preview |
| Incomplete | Missing metrics/gaps and `reasons`; never substitute zero | Reads and otherwise valid actions |
| Read-only | Persistent scope banner | Reads, filters, pagination; no activation/mutations |
| Suspended | Reason/code and new-exposure prohibition | Reads and backend-permitted close/cancel/protection actions |
| Retired | Historical/read-only banner | Preserved local reads only |
| Failed startup | Sanitized code on selection card | Return/select another trader; no runtime actions |
| Expired preview | Review marked expired, submit disabled | Return to preserved draft and create a new preview |
| Disconnected | Connection banner; freeze UI state | No new server work; already dispatched command continues server-side |

Also distinguish collector `refreshing`, command `submitting`, acknowledged/pending execution,
unknown execution, and application `stopping`. Stale data is not the same as disconnected;
incomplete data is not the same as empty.

## Implementation sequence

Implement in the following gated stages:

1. **Catalog and runtime foundation**
   - add the optional dependency/entry point, strict TOML parser, single-environment runtime
     registry, one-runtime start/reuse logic, event-loop offloading, broker gate, and graceful
     shutdown;
   - complete the service/dbzero concurrency hardening and tests before enabling more than one
     workspace.
2. **Native command integration**
   - add typed requests/results, server-held prepared commands and idempotency keys, activation,
     submission, capability classification, and explicit reconciliation to `ui_api`;
   - prove the existing CLI and direct `TraderService` interfaces are unchanged.
3. **Shell and read views**
   - build the persistent `ui.sub_pages` shell, per-tab workspace, status timer, browser-safe
     presentation conversion, dashboards, statistics/chart, bounded lists, detail drawers, and
     refresh-preservation behavior.
4. **Manual workflows**
   - implement activation and all edit/preview/review/submit/outcome dialogs, lifecycle action
     gating, expiration/invalidation, duplicate-click behavior, unknown outcome treatment, and
     explicit execution checking.
5. **Lifecycle and browser verification**
   - add disconnect/navigation disposal, shutdown/drain tests, responsive/accessibility checks,
     and NiceGUI browser interaction tests with fake brokers.

Do not merge a page that calls current `TraderService` command methods concurrently and defer
the hardening to a later stage.

## Test and acceptance plan

### Unit and native API tests

Add deterministic tests for:

- catalog schema, duplicate keys/traders, filename containment, unknown fields, catalog/CLI
  environment mismatch, and profiles whose verified environment is wrong;
- one runtime and collector under simultaneous first opens, runtime reuse across tabs, and
  isolation when tabs select different traders;
- typed request validation and conversion of local position/order/preview handles, including
  rejection of foreign-session, foreign-trader, stale, and wrong-type handles;
- server-held idempotency keys, input invalidation, preview expiry, and two submit callbacks
  producing one durable intent/broker dispatch;
- every market, market-if-touched, limit-IOC, cancel, rate/trailing protection, partial-close,
  and full-close request using fake broker capabilities;
- known rejection, acknowledgement/pending, unknown outcome, lookup miss, and explicit
  reconciliation without automatic resubmission;
- copy divergence blocking open but not otherwise permitted close/cancel/protection actions;
  read-only, suspended, retired, and uninitialized availability;
- dashboard/statistics rendering with every `Decimal | None` combination, stale refresh,
  completeness reasons, and UTC today/week/month rollover;
- chart automatic resolution, partial buckets, missing metrics, visible missing-bucket gaps,
  and rejection beyond the 1,000-bucket limit;
- indexed filters, frozen cursor pages, bounded page sizes, and no totals derived from the
  displayed page; and
- generation refresh that preserves drafts, filters, selected rows where valid, and current
  table position or shows the new-data indicator.

### Concurrency and lifecycle tests

Use blocking fake broker calls and barriers to prove:

- the NiceGUI event loop remains responsive while preview, dispatch, reconciliation, and
  collection are blocked;
- commands for one trader are serialized and cannot overlap its collection broker call;
- commands for separate traders can perform network work concurrently while dbzero critical
  sections remain serialized and short;
- exactly one collector exists for a trader shared by multiple tabs;
- stale callbacks and late results cannot update a new route/trader;
- disconnect/navigation disposes timers and read callbacks but does not cancel an enqueued or
  dispatched submission; and
- shutdown rejects new work, drains submitted commands, joins collectors, closes brokers and
  executors, then closes dbzero once without losing the durable in-flight result.

### Browser interaction tests

Run NiceGUI browser tests against fake services/brokers. Cover two independent tabs, trader
switching in only one tab, persistent shell navigation, responsive tables/drawers, activation,
all manual workflows, double-click submit, expired previews, disconnected overlays, pending and
unknown outcomes, reconciliation, and preservation of edits/pagination during refresh.

Inspect browser network payloads and rendered DOM in tests. Assert that they contain no
credential/profile value, dbzero prefix, database UUID, cursor internals, memo representation,
broker payload, or idempotency key. Catalog keys, labels, formatted financial values, safe error
codes, and informational broker entity numbers are the only intended identifiers.

### Regression gates

Acceptance requires:

- existing CLI/service tests and compatibility behavior;
- strict typing for `trader_api`, lint, and all backend tests;
- native `ui_api` tests plus the new typed-command tests;
- NiceGUI browser interaction tests with fake brokers; and
- no real-money or external broker mutation in automated tests.

### dbzero validation prerequisite

The corrected Python 3.13 `dbzero-pro` package is a blocking prerequisite, independent of UI
correctness. Before UI implementation is accepted, the exact pinned wheel used by the project
must pass both:

1. an isolated temporary-root reproduction that persists timezone-aware `datetime` values,
   closes/reopens dbzero, and verifies native `datetime` type, UTC awareness, and value at
   dbzero's documented millisecond precision; and
2. the repository's native model and UI API suites, including at minimum
   `tests/test_native_models.py` and `tests/test_ui_api.py`.

Record the wheel identity and test output in the implementation change. This document does
**not** assert that the local rebuild referenced by `pyproject.toml` has passed that gate; its
presence or version string alone is insufficient evidence.

## Definition of done

The UI is complete only when a user can open any valid catalog trader in two tabs, work in each
tab independently while sharing one verified runtime, inspect all persisted financial and audit
views without identifier leakage, explicitly activate an eligible existing binding, safely
preview and submit every supported manual operation, distinguish every command outcome, and
explicitly reconcile ambiguous executions. The server must remain responsive during broker
work and shut down in the required order without abandoning submitted commands. All validation
and regression gates above, including the separately evidenced dbzero prerequisite, must pass.
