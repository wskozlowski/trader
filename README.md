# Agentic trader service

This package implements the isolated trader contract in `designs/trader-api-design.md`. It never
turns a locally entered balance into broker money. Bound traders use an owner-registered Agent
Portfolio and copy mirror. Explicit standalone traders execute directly in the credential's broker
account, with a virtual strategy-only cap and no owner mirror.

## Setup

The local dbzero 0.6.6 wheel is installed from `/dbzero-data/lib` and requires
regular CPython 3.14 (`3.14+gil` in uv).

```bash
uv sync --all-extras --python 3.14+gil
chmod 600 .env_demo
.venv/bin/trader --help
.venv/bin/trader-admin --help
```

Profiles are fixed project-root filenames and are never merged with ambient variables. Remove the
obsolete `TRADER_ENV`; only broker/owner-verified scopes establish an environment. The committed
`.env_demo.example` contains supported full demo URLs. Never commit an actual profile.

For direct demo trading, explicitly set `TRADER_TRADING_MODE=standalone` in a mode-0600 profile,
use direct-account credentials, and configure identity/P&L, eligibility, quote, cost, and execution
routes for the same environment. Startup authenticates the broker read, binds ordinary-account identity
to a one-way fingerprint of the authenticated user key (or explicit broker IDs when returned), and probes
ETH eligibility before any command is enabled. No Agent Portfolio or copy relationship is created. Broker write access
remains authoritative at dispatch; a successful read does not guarantee that an order will be accepted.
The `init` amount is strategy capital only, not a broker deposit. No agent binding or copy is created.

```bash
trader --config .env_demo --trader alpha --expected-environment demo \
  smoke-demo --expected-investment 1000 --symbol ETH --strategy-notional-usd 100
```

Repeating the same smoke command reports its durable outcome without a second broker dispatch.
If it reports an unknown or pending execution, explicitly repeat with `--reconcile` to look up the
same order; do not place a replacement trade. The smoke command refuses REAL and bound profiles.
REAL standalone accounts require an explicit profile and catalog opt-in; there is no automatic smoke.

## Provisioning and worker isolation

`OwnerAdminService` is a separate authenticated surface. Its `register_existing` method consumes
issuance/owner evidence bound to the exact child token. Trader `init` only asserts an existing owner
investment and activates that binding.

Bootstrap local owner/vault secrets once, then provision through the separate admin entry point. The
child profile is created with mode 0600 and the one-time broker token is also encrypted in the fixed
demo storage root. Reusing the administrative key never sends a second provisioning request.

```bash
.venv/bin/trader-admin --config .env_demo bootstrap
.venv/bin/trader-admin --config .env_demo provision \
  --trader <immutable-trader-id> --investment-usd 1000 \
  --portfolio-name <6-to-10-char-name> --token-name <token-name> \
  --administrative-request-key <durable-key> --child-config .env_demo_child
.venv/bin/trader --config .env_demo_child --trader <immutable-trader-id> \
  --expected-environment demo init --expected-investment 1000
```

The current broker create contract has no resting limit order. `limit_ioc` executes immediately or
cancels immediately and its rate must be within 10% of market. `market_if_touched` may rest at a
remote trigger but becomes a market order when triggered. Callers must choose explicitly between
those semantics; the CLI does not relabel MIT as a limit order.

Production deployments instantiate `TraderService` in an `IpcWorker`. Its Unix socket is mode 0600,
requests are HMAC-authenticated, methods are allowlisted, and only values cross the boundary. Run the
coordinator and each trader worker under separate service accounts. The coordinator owns the control
prefix; a trader worker owns only its assigned prefix.

Physical roots are fixed at `/dbzero-data/trader-dev` for demo and `/dbzero-data/trader` for real.
Tests may use child directories beneath the demo root. No dotenv, environment variable, or CLI option
can redirect the real root.

Persistent models use plain names (`Intent`, `Preview`, etc.) and store related memo objects
directly. `Trader` owns the immutable name in its trader prefix; `TraderState`,
`PortfolioBinding`, and the control-side `TraderRegistration` reference that instance.
String trader names are resolved during registration/authentication and retained in API values.
Models use dataclass constructors without prefix arguments; storage operations
select the active prefix with `db0.open` before constructing objects, including singletons.
Monetary amounts, position units, and protection rates are stored as `Decimal` values;
JSON/API output serializes them as strings.
Parameters and audit facts use native dictionaries, and broker outcomes retain native
numeric values. Timestamps use timezone-aware `datetime` values; audit hashing uses UTC
at dbzero's millisecond precision. Operations, lifecycle states, execution states, sides,
and ledger categories use dbzero enums. Absent optional values use `None`.
UUIDs are serialized only for API responses and exports; incoming API IDs are
resolved to instances with type and trader-prefix checks. Internal links, comparisons, and
relationship queries use instances, with no UUID lookup tags. `tag_fields` indexes relationships and broker IDs,
with object-tag queries using `db0.as_tag(instance)`. Broker/request IDs and opaque trader
storage keys retain their external identity and routing roles.
Audit hashes bind the referenced intent's command digest.

This storage schema replaces the former `*Memo` classes and ID fields. Existing databases
require migration before use; no automatic migration is included.

## Recovery, backup, and replay

Submission uses four durable barriers: trader intent/reservation, control reservation, control
outcome, and trader projection. An ambiguous broker mutation stays `UNKNOWN` and holds reservations.
Recovery looks up opens by their committed request UUID and never treats a miss as non-execution.

A strategy fill makes mirror state pending until the owner reader records the actual mirror outcome.
The proportional estimate is only a conservative admission bound and never becomes an owner fill.

`DbzeroStore.backup_prefix` creates a committed per-prefix dbzero copy beneath the same environment
root. Restore into a new isolated child directory, verify the audit chain, and reconcile before
allowing exposure. Back up the control prefix separately. Retiring broker portfolios remains an
explicit owner operation and is not performed by disable, restore, or local cleanup.

## Checks

```bash
.venv/bin/ruff check .
.venv/bin/mypy trader_api
.venv/bin/pytest -m 'not external_demo'
.venv/bin/pytest -m external_demo
.venv/bin/pytest --cov=trader_api --cov-report=term-missing
graphify update .
```

The external demo suite is opt-in. It uses dedicated portfolios, no more than USD 2,000 initial
investment, leverage one, and the smallest eligible size. Missing owner/child binding evidence is a
blocked test, not a pass. Every external resource is tracked; ambiguous mutations are never blindly
retried and portfolio deletion requires separate explicit authorization.

## Web UI

Run `./start_ui.sh` and open `http://localhost:8001`. The launcher syncs the UI dependencies,
including the local dbzero wheel, into the project environment.

The UI uses NiceGUI 3.18.0, which removes the older `vbuild` dependency incompatible
with Python 3.14. Trader initialization and dashboard reads run in worker threads so dbzero transactions stay outside NiceGUI asyncio tasks.
Startup, dashboard, and background portfolio refresh errors print to the server console
with tracebacks; the browser displays a stable error code.

The browser regression test uses an isolated database and simulated broker without external
trading. Install `playwright` with `uv pip install --python .venv/bin/python playwright` and run `.venv/bin/python -m playwright install chromium`,
then `.venv/bin/python -m pytest tests/test_web_ui.py`. NiceGUI must also be installed.

The workspace shows only the selected trader’s durable local positions, pending orders,
and execution history. Account cash, external positions, and old account-wide valuation
snapshots do not enter its dashboard. **Allocated capital** is strategy capital, not a
broker deposit; **Available strategy budget** follows the existing admission rules.
Uninitialized traders can initialize their allocation in the workspace.

Open orders default to market and leverage one. Market-if-touched and limit IOC are
available when supported by the configured broker. Review the trader, environment,
instrument, size, estimated costs, and budget impact before submitting. Positions
support full or partial closing (default 100%); owned pending orders support cancellation
when the corresponding broker route is configured. Editing a draft or switching workspaces
invalidates its review. Repeated submission uses the same idempotency key. Pending or
unknown executions are reconciled through their existing records, without replacement
orders, and dispatched work continues if the browser disconnects.

P/L uses confirmed fills and costs. Realized P/L is gross confirmed close profit minus
confirmed execution costs, including opening fees once. Unrealized P/L uses remaining
confirmed units and bid for longs or ask for shorts; leverage is not applied again.
Missing required execution, cost, or price data displays **Waiting**. An initialized
trader with no executions has an empty portfolio and zero P/L.

Prices refresh every 60 seconds, after execution changes, and on manual request (subject
to broker retry backoff). Cached prices survive errors and restarts. Each position shows
its last successful quote timestamp; it turns red only after 90 seconds. Aggregate P/L
uses the oldest contributing price timestamp. Reloading the page does not refresh that
timestamp. Confirmed fills and prices use separate persisted records, preserving existing
databases; execution backfill looks up only locally recorded orders.

Commands serialize per trader, with broker I/O outside database critical sections.
Persistent model classes use empty Python slots to avoid inheriting managed instance
dictionaries and weakrefs into dbzero's native memory layout on CPython 3.14. This
preserves stored fields and existing databases. Database critical sections also defer
automatic cyclic garbage collection while the pinned native extension releases the GIL,
restoring the previous GC setting immediately afterward. Trading tests use simulated brokers, including two traders sharing an account and
unrelated external positions.

Validation limitation: the separate `test_dbzero_repro.py` repeated close/reopen test
still reproduces an intermittent segmentation fault inside dbzero 0.6.6, including
native Decimal decoding. The model-layout and GC safeguards above are mitigations,
not a complete fix for that dependency defect. The standard non-external suite and
browser scenarios have passed, but native restart stability remains unresolved.
