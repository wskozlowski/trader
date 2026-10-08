# Agentic eToro trading CLI and Python API

## Purpose and boundaries

`trader` is a Python service library and machine-readable CLI for isolated trading agents. Each trader controls one eToro Agent Portfolio through its own broker-issued user token. An owner separately allocates money to copy that portfolio. Local bookkeeping is not a substitute for this broker relationship.

This revision is grounded in the official portal and the explicitly authorized demo validation on
2026-10-07. “Documented” below means specified by eToro; the later implementation-validation section
separately identifies behavior verified with the dedicated demo Agent Portfolio.

The previous revision incorrectly treated the owner's investment as the agent's trading balance and excluded copy trading. The corrected design includes the copy relationship required by Agent Portfolios; general social copy discovery, arbitrary transfers and withdrawals remain outside the agent interface.

## Documented broker model

[Create Agent Portfolio v2][create-agent] takes `investmentAmountInUsd`, a portfolio name, a token name and `scopeNames`. The investment funds the owner's copy relationship; the agent instead receives a separate virtual balance. Persist the returned `agentPortfolioId`, `agentPortfolioGcid`, `agentPortfolioVirtualBalance`, `mirrorId` and token metadata. Do not assume the virtual balance equals the investment or hard-code its example value.

Thus there are two accounting domains: agent strategy activity and owner mirror activity. The initial investment/virtual-balance ratio explains proportional sizing, but is not a permanent execution guarantee. Never derive actual owner fills or P&L solely by multiplying agent results.

[Get Agent Portfolios][list-agent] lists the owner's portfolios and token metadata, including named scopes. Use it for administrative discovery and reconciliation, filtering before any trader-facing projection. The Agent Portfolio UUID, agent GCID, numeric trading portfolio ID and owner mirror ID are different identifiers; verify their relationship rather than equating them.

The application offers capped owner allocation and stricter local exposure policies. The reviewed Agent Portfolio endpoints do not establish a configurable maximum-loss or notional-cap setter. Do not invent one, or describe invested capital as guaranteed maximum loss.

## Permissions and provisioning

Use `https://public-api.etoro.com` with endpoint-specific versions. The selected authentication mode is the application `x-api-key` plus the appropriate `x-user-key`, with a UUID `x-request-id`. Owner credentials are used only by the provisioning/reconciliation service; agent-portfolio user tokens are used for strategy execution. Agent callers receive neither. Environment-specific keys are required; a virtual agent balance does not by itself mean demo mode. See [Authentication][auth].

Owner operations are separate from trading:

| Responsibility | Official operation |
| --- | --- |
| Provision portfolio and initial token | `POST /api/v2/agent-portfolios` ([reference][create-agent]) |
| Discover assignable scopes | `GET /api/v2/agent-portfolios/user-tokens/scopes` ([reference][allowed-scopes]) |
| List owned portfolios/token metadata | `GET /api/v1/agent-portfolios` ([reference][list-agent]) |
| Issue replacement token | `POST /api/v2/agent-portfolios/{agentPortfolioId}/user-tokens` ([reference][create-token]) |
| Change token restrictions | `PATCH /api/v2/agent-portfolios/{agentPortfolioId}/user-tokens/{userTokenId}` ([reference][update-token]) |
| Retire broker portfolio | `DELETE /api/v1/agent-portfolios/{agentPortfolioId}` ([reference][delete-agent]) |

The v2 scope vocabulary includes `etoro-public:trade.real:read`, `etoro-public:trade.real:write`, `etoro-public:trade.demo:read`, and `etoro-public:trade.demo:write`. Discover allowed names and grant only the selected environment's required pair; do not send deprecated numeric scope IDs. Allowed scopes are a grant catalog, not proof of a particular token's current permissions. Confirm issued metadata and successful scoped reads. The owner's UI “Agent portfolio” permission is distinct from the child token's trade scopes; its exact administrative authorization requirements still need account verification.

[Token creation][create-token] returns a secret available only at creation. Persist it securely before activation; never put raw create responses in audit or logs. A crash after broker creation but before secret persistence requires discovery and controlled token replacement, not another funded portfolio. [Token updates][update-token] support scope names, IP allowlists and expiry. Changed/expired/revoked credentials suspend new exposure and invalidate previews. Do not switch to owner credentials to bypass a restriction. Permitted closes/cancels remain available; if authorization prevents them, report owner action required.

Application binding:
`(environment, owner_account_id, agentPortfolioId, agentPortfolioGcid, agent_trading_account_id, agent_trading_portfolio_id, mirrorId, trader_id)`.
Bind each active portfolio to one trader. Keep credentials as secret references and scope snapshots in control storage. Runtime methods cannot accept caller-selected broker identities.

Lifecycle: `UNBOUND -> PROVISIONING -> VERIFYING -> READY -> ACTIVE -> SUSPENDED -> RETIRED`. Owner provisioning approves the copy investment and local policies; `READY` requires reconciled identity, token, empty strategy baseline and the correct owner mirror. Agent `init` activates this existing binding. It never creates a portfolio, invests money or grants permissions. Unknown provisioning outcomes stay blocked until reconciled. Administrative actions have their own durable request keys and recovery state.

Retirement is not a normal “disable” operation: [deletion][delete-agent] revokes tokens, stops the mirror and removes broker storage. Preserve local history, reconcile positions/settlement, and require explicit owner authorization before invoking it. The exact settlement consequences must be verified before offering automated retirement. Neither disabling an agent nor revoking a token proves its existing positions are closed.

## Trading adapter: documented mappings

Select the current documented v2 order contract initially; later v3 adoption is a separate versioned change. Older dedicated open-order routes are deprecated in the [documentation index][index]. Internal `preview`, `permissions` and `limits` methods are application abstractions, not invented eToro endpoints.

| Service operation | Broker mapping / design consequence |
| --- | --- |
| Open real strategy position | `POST /api/v2/trading/execution/orders`; [create order][open-real]. `action=open`, long maps to `buy`, short to `sellShort`. |
| Open demo strategy position | `POST /api/v2/trading/execution/demo/orders`; [demo create][open-demo]. Maintain an explicit per-operation environment route table. |
| Inspect/recover order | `GET /api/v2/trading/info/orders:lookup`; [lookup][lookup]. Query by order ID or submission request UUID as `referenceId`, not both. |
| Cancel opening order | `DELETE /api/v2/trading/execution/orders/{orderId}`; [cancel][cancel]. Reconcile fill/cancel races. |
| Close/partial close | `POST /api/v1/trading/execution/market-close-orders/positions/{positionId}`; [close][close]. Normalize fractions to `UnitsToDeduct` and `InstrumentId`; verify precision and remaining units. |
| Change protection | `PATCH /api/v2/trading/positions/{positionId}`; [modify][modify]. Use documented stop-loss/take-profit rate and stop-loss-type fields. No implied leverage edit or arbitrary pending-price edit. |
| Strategy and mirror snapshots | `GET /api/v1/trading/info/real/pnl` or `/api/v1/trading/info/demo/pnl`; [PnL][pnl], [equity guide][equity]. Strategy reads use the child token; owner reads are isolated and projected only for the bound mirror. |

The create contract supports `mkt`, `mit` and `limitIOC`, not generic interchangeable limit/stop semantics. MIT triggers a market execution; it is not a guaranteed limit fill. Provide exactly one symbol/instrument ID and one supported sizing representation. `amount` is investment amount, not our full-notional field. USD is currently the order currency. Leverage above one requires a stop-loss rate. The adapter must normalize and validate these distinctions before dispatch. See [order schema][open-real].

Both [v2 create][open-real] and [v3 asynchronous submission][open-v3] list enum values for close directions while their descriptions say only opening is currently supported. Do not enable operations just because an enum includes them. v3 also changes settlement-type validation: MIT omits it; other types require an eligible value. Validate instrument/direction/leverage/settlement combinations and keep v2/v3 rules separate. `limitIOC` requires a positive `limitRate`, restricted to within 10% of the current market price; omit that field for other types. Validate eligibility and execution behavior before enabling it for a particular binding.

Demo routes for close/cancel/modify/lookup are documented in the configuration table below; do not generate them by string substitution. Agent Portfolio mirroring behavior still requires integration verification. Unsupported operations fail before preview. Account eligibility, token scope and local policy can further restrict documented capabilities.

## Runtime, trust boundary, and storage

The executable and importable library share one `TraderService` implementation and one account-level execution coordinator with a validated broker adapter; the CLI is not a second trading implementation. A trader authenticates with a service credential bound server-side to exactly one immutable `trader_id`; a CLI `--trader` value is only an explicit routing assertion and must match the authenticated identity. Traders never receive eToro credentials or direct access to the coordinator. Use `Decimal` for money, UTC timestamps, USD as the explicitly supported integration policy currency, and explicit rounding up for estimated costs/reservations. Never use binary floating-point arithmetic for budget decisions. Broker and service configuration comes from the selected environment's fixed dotenv file, described below, and never appears in command arguments, JSON output, memo objects, or audit payloads. Agent callers may invoke only their constrained service API, not arbitrary dbzero mutations, other prefixes, the control prefix, or the raw broker client.

The coordinator is the sole application broker writer. It resolves the immutable portfolio binding, serializes submissions/capacity checks within each portfolio, and coordinates owner-level provisioning or shared restrictions where the verified broker contract requires it. It cannot serialize external manual eToro activity; broker enforcement and reconciliation must cover those races. It may know that traders exist and maintain routing data, but never exposes one trader's identity, capital, positions, results, capacity consumption, or errors to another. Trader-facing errors such as `BROKER_CAPACITY_UNAVAILABLE` reveal no account totals or competing activity. A separate administrator role may inspect account-wide coordination and reconciliation data; trader credentials cannot use that interface.

In real mode, run the coordinator and each trader data worker in separate service security contexts. A trader worker opens only its assigned prefix read-write; the coordinator opens only the control prefix read-write and passes immutable scalar commands/results to the correct worker. An administrator's aggregation process opens trader prefixes read-only. This makes the prefix boundary enforceable rather than depending on every query remembering a trader filter.

### Credential-derived environment

This revision supersedes default-demo environment selection, mandatory `TRADER_ENV`, and mandatory `--env real`. The **user key** (`x-user-key`), not the application key (`x-api-key`), establishes the trading environment through verified granted scopes. A filename, URL, token prefix, unverified decoded token, or configured scope string cannot establish it.

Load one explicit credential/configuration profile first: `--config .env_demo` or `--config .env`, resolved from the fixed project root; omission loads exactly `.env`. These filenames select credentials, not environments. Never merge profiles, search the current working directory, inherit missing values from another file, or fall back after an error. Profile files use the same typed schema for credentials/secret references, expected identity, service authentication, timeouts and explicit operation URLs. The example below supplies only the credential/endpoint portion of that schema.

Resolve startup in this order:

1. Parse the selected file and validate every URL against the pinned official host/method/path allowlist before attaching credentials. Reject redirects, unexpected ports, embedded credentials, fragments, path traversal, mixed-environment route sets and unknown placeholders. Ambient process variables cannot override file values.
2. Obtain broker-issued scope metadata bound to this exact user token. For managed Agent Portfolio tokens, use securely retained issuance metadata and refresh current token metadata through the separately authenticated owner listing. The assignable-scopes endpoint is not token introspection. The portal does not establish a general introspection endpoint for arbitrary manually generated keys; such keys need a verified provisioning record or a subsequently verified official discovery mechanism. Do not invent an endpoint or treat a user-written scope list as evidence.
3. Demo trading scopes only resolve `demo`; real trading scopes only resolve `real`. Neither family resolves `ENVIRONMENT_UNVERIFIED`; both resolve `AMBIGUOUS_KEY_ENVIRONMENT`. Read-only scopes can establish environment without enabling writes. No command-line choice resolves ambiguity. Expiry/revocation invalidates the verification record.
4. Require the configured operation routes to match that environment, then perform only its configured read-only P&L/identity validation. A successful read corroborates access and identity, but does not prove exclusive environment scopes or write permission. A failure, timeout or 401 is not evidence for the opposite environment. Never probe trading mutations or fall back to another environment.
5. Verify the child binding and owner/mirror environment agree, then open the corresponding fixed dbzero root. Authenticate required owner credentials separately; child demo scope alone does not prove the copy investment is demo. Freeze the verified environment, identities, credential fingerprint, routes and policy in the runtime context.

| Verified user-key environment | Required dbzero root |
| --- | --- |
| `demo` | `/dbzero-data/trader-dev` |
| `real` | `/dbzero-data/trader` |

The root cannot be overridden from dotenv, process environment or CLI. Configuration changes require restart. Rotating a key to a different environment cannot migrate an initialized binding or its pending intents. Audit only redacted fingerprints and verification facts, never secrets. `--env`, if retained for compatibility, is an optional equality assertion against the resolved environment, never a selector. Reject obsolete `TRADER_ENV` configuration with migration guidance. Real-scoped credentials plus a matching real route profile need no additional `--env real`; a real key in the demo-only profile fails with `ENDPOINT_ENVIRONMENT_MISMATCH` and never rewrites its URLs.

### Explicit demo-only endpoint configuration

These are proposed application configuration names, not existing prototype support. Set them in the selected profile (for example `.env_demo`); no `TRADER_ENV` is needed. Each operation uses a full URL; no separate base URL is configured. The adapter validates each URL against its internal allowlist for the trusted hostname `public-api.etoro.com` and documented operation paths. Templates use literal `{orderId}` / `{positionId}` with validated numeric substitutions; lookup query parameters are encoded separately.

```dotenv
ETORO_API_KEY=<application-key>
ETORO_USER_KEY=<demo-user-key-or-resolved-child-token>
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
```

| Configuration suffix | HTTP method | Official demo reference |
| --- | --- | --- |
| `PNL_URL` | GET | [P&L/equity guide][equity] |
| `OPEN_ORDER_URL` | POST | [Create order][open-demo] |
| `ORDER_LOOKUP_URL` | GET | [Order lookup][demo-lookup] |
| `CANCEL_ORDER_URL` | DELETE | [Cancel opening order][demo-cancel] |
| `CLOSE_POSITION_URL` | POST | [Close by units][demo-close] |
| `CLOSE_ORDER_LOOKUP_URL` | GET | [Close-order lookup][demo-close-lookup] |
| `CANCEL_CLOSE_ORDER_URL` | DELETE | [Cancel closing order][demo-cancel-close] |
| `MODIFY_POSITION_URL` | PATCH | [Modify TP/SL][demo-modify] |
| `TRADING_HISTORY_URL` | GET | [Trading history][demo-history] |
| `TRADING_COSTS_URL` | POST (what-if query) | [Costs][demo-costs] |
| `TRADING_ELIGIBILITY_URL` | POST (eligibility query) | [Eligibility][demo-eligibility] |

The HTTP methods are fixed in the adapter, not configurable. No v3 routes are mixed into this v2 opening contract. Missing required routes disable the corresponding capability; no generated defaults to real routes. Market-data routes are environment-neutral and may be separately allowlisted. Agent Portfolio administration routes are also not labeled demo-only in the portal: keep them in the owner provisioning profile, outside this runtime route set, until demo provisioning/copy semantics are verified. This template does not by itself provision an agent or supply its verified identity/scope metadata.

Treat `.env` and `.env_demo` as secrets: keep them ignored by Git, owned by the service account, and readable/writable only by that account (normally mode `0600`). Commit only redacted `.env.example` and `.env_demo.example` templates if templates are needed. Never show file contents through diagnostics, `--json`, exceptions, process listings, or audit exports. The files configure the shared eToro account and service; per-trader balances, portfolios, registrations, and authorization bindings remain in their isolated dbzero/control partitions rather than in dotenv.

Configure dbzero's **physical root** as the corresponding absolute path. Inside it, create one control prefix and one dedicated prefix per trader:

```text
/trader/{environment}/control
/trader/{environment}/traders/{storage_key}
```

Each prefix is a separate dbzero data partition with an independent commit history and backing dbzero file set. The trader prefix is that trader's complete view: all portfolio, order, balance, intent, ledger, snapshot, and audit memos for two different traders must never occupy the same prefix or backing data file. `storage_key` is a fixed opaque UUID or keyed digest assigned by the coordinator, never a raw user-controlled path segment; the private control prefix maps it to `trader_id`. At startup, assert that every discovered trader prefix maps to one and only one registered trader and that no trader model exists in the control prefix. Do not use cross-prefix memo references or `db0.weak_proxy()` between trader and control data; exchange immutable scalar command/result messages through the service boundary instead. Backups and exports preserve the same per-prefix isolation.

A logical prefix is not a substitute for the physical root. Every dbzero-managed file, index, journal, snapshot, and test fixture belongs inside the selected environment root (tests use isolated subdirectories under `/dbzero-data/trader-dev`). Resolve and validate paths, including symlinks, before opening the store; reject a root outside its allowlisted directory, an environment/account mismatch, or an attempt to change the initialized account. Do not rely on the working directory or a user-provided arbitrary `--db-path`. Provision the demo directory before use if it does not exist. Keep separate broker credentials and never copy real-account memo data into demo.

`--help`, `version`, per-trader `capabilities`, `status`, and `init` may run before activation. `init --expected-investment 2000.00 --currency USD` asserts the previously approved owner copy investment, not the agent virtual balance. Verify both accounting baselines and the issued child identity/token before committing activation. Owner provisioning serializes initial investment commitments to avoid promising the same cash twice. Discovering an existing mirror must not debit local owner cash again. Persist binding/storage registration before the trader baseline; repair any incomplete activation saga before accepting trades.

`init` is single-use per environment/trader/binding; identical retries are idempotent and different identities, mirror, investment or currency fail. Agents cannot top up themselves, alter copy investment, create tokens, change permissions or transfer positions. Owner-side deposits do not credit strategy policy. Keep the main account and unrelated manual positions outside agent views. Owner retirement never deletes local audit history or releases unresolved risk.

## Persistent object graph and audit trail

All durable application entities are dbzero memo objects: ordinary Python classes decorated with `@db0.memo(...)`, assigned at construction to either the caller's trader prefix or the private control prefix, and using references only to objects in the same prefix. Follow the conventions in `/src/selltime/AGENTS.md`: singleton root where appropriate; `db0.tags(obj).add(...)`, `db0.find(Class, ...)`, and indexed queries instead of Python-side scanning. The following is the logical schema, not a promise of unverified decorator or broker field signatures:

| Memo type | Durable content and relationships | Useful tags/indexes |
| --- | --- | --- |
| `TraderState` singleton and `BudgetPolicy` (trader prefix) | Trader ID, environment, currency, separate strategy/owner initial policy caps, policy version, reconciliation cursors; immutable initialization parameters; no main-account or other-trader snapshot | trader, environment |
| `TradeIntent` and `Reservation` (trader prefix) | Requested operation, normalized parameters, preview expiry and opaque broker-state fingerprint; trader-scoped submit key, full notional plus cost buffer, expiry and terminal resolution; references to local state and target | trader, state, key, expiry |
| `BrokerOrder` and `Position` (trader prefix) | Only broker entities owned by this trader: broker IDs, parent intent, symbol, direction, leverage/notional, fills, cost basis, TP/SL, remaining exposure and last observed version | symbol, state, broker ID |
| `AuditEvent` and specialized event subclasses (trader prefix) | Immutable trader-local sequence, UTC time, actor/request/key, local references, before/after facts, source (`trader`/`broker_manual`/`reconcile`), policy decision, redacted broker metadata, and previous-event hash | time, operation, entity, source |
| `LedgerEntry` and `PortfolioSnapshot` (trader prefix) | Immutable principal release/reservation, net realized P&L and costs; valuation of only this trader's cash allocation and owned broker entities | time, event |
| `AccountControl` and `TraderRegistration` (control prefix) | Main broker account binding; opaque trader/storage mapping; authorized/disabled state; approved copy investment and aggregate provisioning accounting, without trader strategy or portfolio projections | trader hash, storage key, state |
| `OwnershipClaim` and `ControlEvent` (control prefix) | Portfolio-scoped broker entity key to opaque owner mapping, portfolio submission reservation, reconciliation cursor, command digest, state and outcome needed for serialization/recovery | scoped broker ID, owner key, global request/key, state |

Extend the schema with `PortfolioBinding`, `PermissionSnapshot`, `ProvisioningIntent`, and `MirrorReconciliation`. Store all distinct broker identities, owner-approved investment, initial strategy virtual balance, independent risk-policy caps, token reference/metadata, lifecycle and observed copy state. Persist separate strategy and owner-mirror ledger/snapshot domains in the trader's own prefix; full owner snapshots remain private. Every intent and reservation carries binding/policy versions. Broker identity keys are `(environment, trading_account, trading_portfolio, entity_type, broker_id)`; a bare broker ID is insufficient.

For example, these are regular Python classes whose fields can reference other memos in the same prefix. `db0.init(dbzero_root=...)` selects the physical root and `db0.set_prefix()` assigns each instance dynamically to the isolated trader partition. The service derives `trader_prefix`; callers never supply it:

```python
import dbzero as db0

@db0.memo(singleton=True)
class TraderState:
    def __init__(self, trader_prefix, trader_id, strategy_cap, owner_cap):
        db0.set_prefix(self, trader_prefix)
        self.trader_id = trader_id
        self.strategy_initial_policy_cap = strategy_cap
        self.owner_initial_policy_cap = owner_cap

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

Use inheritance for operation-specific events (for example, open, fill, modify, cancel, close, manual override) while keeping the common audit fields/query interface in `AuditEvent`. Verify the installed dbzero memo-inheritance and dynamic-prefix behavior before writing subclasses; if that version cannot persist either safely, persist one immutable memo event type with a discriminated `kind` and explicitly set its prefix while retaining the same public event contract. Same-prefix references and tags link every event to the initiating intent and broker entity. The control prefix stores only scalar trader/storage keys and command digests, never references into a trader prefix. Ownership keys use the scoped broker identity tuple above; idempotency keys are unique within `(environment, trader_id, binding_version, key)`. Never treat a mutable display symbol as an identity.

Audit records and accounting entries are append-only; status is derived from events or maintained as a rebuildable projection. Do not edit/delete audit events to correct mistakes: append a correction event linked to the original. Use dbzero immutable memos where the installed version supports them, a monotonic sequence and independent hash chain per trader, plus a separate control-event chain. A trader backup/export contains only that trader's prefix; account administrators may back up the control prefix separately. Periodically retain chain digests externally if stronger tamper evidence is required. A local hash chain detects accidental or selective changes but alone cannot protect against a privileged operator rewriting an entire database. Keep dbzero-generated backup files within the environment root. Archive/export formats must not leak secrets or other traders' identifiers. Document per-trader backup, restore, retention, chain verification, and replay of projections as operational procedures.

The boundary around a broker call is **not** a database transaction, and dbzero does not provide one atomic commit across isolated prefixes. Use a recoverable saga. First commit the local `TradeIntent`, trader reservation, and `INTENT_COMMITTED` event atomically in the owning trader prefix. Then, under the coordinator's portfolio lock (and parent lock only for shared constraints), re-read portfolio state/permissions/cap and commit a control reservation containing the immutable binding/version, opaque owner key, globally scoped command digest, and ownership claim. Only after both explicit `db0.commit()` barriers succeed may the coordinator send the scoped broker mutation. Persist the broker acknowledgment, fill, rejection, or timeout in the control prefix first, then idempotently project that result and linked audit/ledger entries into the owner prefix. No result is ever projected to any other trader. A local intent with no control reservation is safely canceled during recovery; a control reservation/outcome missing from the trader file is replayed from the command digest and result message. Do not rely on periodic autocommit for these barriers.

If dispatch times out or the process crashes, persist `UNKNOWN` and keep reservations. For v2 opens, use the previously committed `x-request-id` as `referenceId` in [order lookup][lookup]; persist returned order/account/GCID/portfolio IDs and executions. This documents correlation, not guaranteed deduplication. A lookup miss is not proof of non-execution. Retry only when non-execution or broker idempotency is established; otherwise quarantine for reconciliation. Close, modify and provisioning calls require their own verified outcome lookup; do not assume open-order recovery applies to them. Never replay under a changed binding or broader credential. Restart must rebuild reservations and reconcile both strategy and mirror outcomes before new exposure.

## Budget and broker-state rules

Use USD for this integration's initial allocation and trading policy. Keep separate ledgers for strategy virtual USD and owner mirror USD. Reject unsupported currencies; do not silently reinterpret the former account-base-currency interface. Use Decimal throughout normalization, conservative reservations and accounting.

Application policy (not a broker field) applies independently to each domain:

```text
capital_ceiling = approved_initial_policy_cap + settled_net_realized_pnl
committed = remaining_entry_notional + unfilled_pending_notional
          + in_flight_notional + unposted_cost_reserves
available_to_open = max(0, capital_ceiling - committed)
```

For the strategy domain, the initial policy cap is owner-approved and no larger than the verified starting virtual balance. For the owner domain, it is no larger than the approved copy investment. Include losses and attributable fees; exclude unrealized gains, short-sale proceeds and external funding. Release entry notional proportionally on partial close. Replace estimates with actual costs once, and exchange pending reservations for fills without double-counting.

An admission decision requires strategy broker capacity, strategy policy capacity, a healthy bound copy relationship, and conservatively bounded owner exposure within its own policy. Neither spare parent cash nor another agent's gains may increase those policies. Full notional includes leverage and contract multipliers. Do not pass public `notional` unchanged as broker `amount`; resolve sizing per eligible instrument/settlement type. Reject an operation when a safe bound cannot be established.

Example of application sizing assumptions: owner investment USD 2,000, reported virtual balance USD 10,000, initial proportional factor 0.2. A USD 1,000 strategy notional suggests USD 200 copied notional before costs. This is a preview estimate, never evidence that a USD 200 owner fill happened. Both domains must independently reconcile actual fills, cash and P&L. Changing copy allocation invalidates the factor and all affected previews; profits cannot justify silently adding owner investment.

Reconcile child strategy and the corresponding owner mirror separately. A strategy fill does not imply a mirror fill: copying may lag, reject, round, or diverge. Track `strategy_execution_state` and `mirror_reconciliation_state`; new risk pauses on unexplained divergence. Do not automatically compensate by placing owner-account orders. Only the trusted owner reader may inspect account-wide snapshots; project the bound mirror into the correct trader prefix without exposing other mirrors.

Manual broker actions supersede the plan. A manual strategy close or owner mirror close/detach invalidates affected intents and triggers reconciliation; never reopen it from an old retry. Manual funding, token changes and mirror investment changes are administrative events, not P&L. Stable attribution is mandatory; never estimate ownership by allocating an account-wide position proportionally among traders.

Broker investment, available margin, local full-notional caps and maximum loss are different concepts. No guaranteed loss isolation is claimed. Gaps, fees and execution can exceed local estimates; a stop-loss does not guarantee an exit price. Automated trading remains disabled until copy scaling and attribution support the required admission checks.

## Public Python and CLI contracts

`TraderService(authenticated_identity, config_profile=".env", expected_environment=None)` binds one authenticated trader and derives its environment from the verified user key. `expected_environment`, when supplied, asserts equality and cannot select routes or credentials. Expose equivalent CLI and typed Python methods:

- Readiness: `capabilities()`, `trader_status()`, `portfolio()`.
- Activation: `initialize(expected_investment, currency="USD")`, asserting an existing owner-approved copy investment.
- Trading: `positions()`, `orders()`, `preview_open(...)`, `preview_modify(...)`, `preview_close(...)`, `preview_cancel(...)`, `submit(preview_id, idempotency_key)`, `intent_status(...)`.
- Recovery/reporting: `reconcile()`, `audit_events(...)`, `portfolio_history(...)`, `trends(...)`, `verify_audit()`.

`capabilities` reports documentation support separately from effective authorization, environment readiness and local policy. Unknown is not allowed. Readiness/status are available before activation without account-wide discovery. Report investment, strategy virtual balance, both local budgets, copy health, scope status and freshness as separate fields; never label all of them “balance”. A nonexistent broker cap is `not_documented`, not zero or unlimited.

Opening input uses `side=long|short`, explicit `order_type=market|market_if_touched|limit_ioc`, and `strategy_notional_usd`. Trigger/limit inputs map explicitly to `triggerRate`/`limitRate` for their respective types. TP/SL inputs are explicitly price rates. Previews show normalized broker amount/units, leverage, full strategy notional, estimated owner copied notional, distinct cost buffers, both remaining budgets, binding/policy versions, expiry and opaque state fingerprint. Caller-supplied estimates do not override service risk checks.

Previews make no broker mutation or permanent reservation. Submit revalidates identity, token scope, copy state, prices and budgets, then commits the two-prefix saga. A stale preview requires a new preview. Idempotency is scoped to environment/trader/binding/key; conflicting reuse fails. Foreign entity IDs return the same `NOT_FOUND` as unknown IDs. Recovered acknowledgments do not mean fills; mirror completion is separate.

Illustrative demo flow after owner provisioning (no commands executed by this revision):

```bash
trader --config .env_demo --trader alpha capabilities --json
trader --config .env_demo --trader alpha init --expected-investment 2000.00 --currency USD --json
trader --config .env_demo --trader alpha portfolio --json
trader --config .env_demo --trader alpha trade preview-open --symbol AAPL --side long \
  --order-type market --strategy-notional-usd 1000.00 --leverage 1 --json
trader --config .env_demo --trader alpha trade submit --preview-id pv_001 --idempotency-key alpha-open-001 --json
trader --config .env_demo --trader alpha trade status --intent-id intent_001 --json
```

Every invocation reports the key-derived environment; before verification it is null/unverified, never implicitly demo. `--trader` is a routing assertion that must match service authentication. The JSON envelope is versioned and includes request/environment/trader/status/data/error; monetary fields are decimal strings. Accepted asynchronous work is `pending`; errors have code/message/retryable/details and nonzero exit status. No secrets, storage paths, foreign entities or account totals appear. Configuration errors additionally include `ENVIRONMENT_UNVERIFIED`, `AMBIGUOUS_KEY_ENVIRONMENT`, `ENDPOINT_ENVIRONMENT_MISMATCH`, and `ENVIRONMENT_ASSERTION_MISMATCH`.

Errors include `NOT_INITIALIZED`, `TRADER_MISMATCH`, `ACCOUNT_MISMATCH`, `PORTFOLIO_NOT_BOUND`, `PORTFOLIO_NOT_READY`, `PORTFOLIO_SCOPE_MISMATCH`, `PERMISSION_REQUIRED`, `PERMISSION_REVOKED`, `PORTFOLIO_SUSPENDED`, `COPY_DIVERGENCE`, `NOT_FOUND`, `UNSUPPORTED_CAPABILITY`, `INSUFFICIENT_BUDGET`, `BROKER_CAPACITY_UNAVAILABLE`, `STALE_PREVIEW`, `IDEMPOTENCY_CONFLICT`, and `RECONCILIATION_REQUIRED`.

History/trends provide separate strategy and owner-mirror series, timestamps, currency, costs, capital flows and completeness gaps. Use broker equity semantics rather than adding uncommitted notional to position market value. [The equity guide][equity] combines available cash, invested capital and unrealized P&L and handles mirrors explicitly. Never add strategy virtual equity to owner equity. Sample at activation, transactions, reconciliation and hourly; preserve missing-data gaps and do not fabricate earlier history. Account-wide views and cross-trader exports require administrator authentication.

## Failure handling, operations, and acceptance

[Rate limits][rates] are keyed by user credential over rolling windows. Enforce the specific endpoint quota groups: current execution references advertise a shared 20/60-second budget, while Agent Portfolio management uses the default shared 60/60-second budget. Endpoint-specific limits take precedence over the overview's general categories. Reserve polling capacity, cache stable instrument metadata, and back off reads after 429. A retry policy must not resubmit an ambiguous mutation.

Use deterministic broker fixtures for local tests and official demo integration for broker validation. Tests belong only beneath isolated directories in `/dbzero-data/trader-dev`; never run automated trading tests against real money. Acceptance requires:

- Two traders with separate child tokens/identities, mirrors and dbzero partitions; overlapping symbols do not cross ownership. Foreign IDs, previews, keys and errors leak no sibling data.
- Distinct owner investment and virtual balance (including unequal values), conservative exposure conversion, leverage/costs, partial fills/closes, and independent realized P&L. Mirror lag/rejection never appears as owner fill success.
- Least-privilege environment scopes, revoked/expired tokens, wrong GCID/portfolio responses, failed copy relationships and owner changes between preview and dispatch.
- Unknown create/token-secret outcomes without duplicate investment; replay at every cross-prefix commit boundary; request-ID lookup after lost acknowledgments; cancellation/fill races and no blind retries.
- Broker amount versus notional, short direction mapping, MIT semantics, required leveraged SL, disabled unsupported order edits, and no v2/v3 schema mixing.
- Key-derived environment independent of filename, no default environment, read-only/unknown/mixed/revoked scopes, assertion mismatches, real key with demo-only routes, credential rotation, no cross-profile merge or route fallback, no mutation probes, fixed dbzero roots, redaction, immutable per-trader audit chains and independent backup/restore.
- Local disable preserves positions/history. Retirement never implicitly calls destructive broker deletion. Revocation that prevents risk reduction reports owner action required.

The portal establishes the architecture and named operations. Remaining **integration gates**, not reasons to invent alternative APIs, are:

| Gate | Evidence still required before enabling affected behavior |
| --- | --- |
| Administrative authorization | Exact owner permission and account eligibility for provisioning; applicable minimum investment and portfolio-count limits. |
| Environment binding | Demo provisioning/copy behavior and separate demo routes for every enabled read/mutation; virtual strategy money alone is not proof of a demo owner investment. |
| Identity/isolation | Child token's actual trading identity; UUID-to-GCID-to-numeric-portfolio mapping; owner mirror attribution; negative cross-portfolio access checks. |
| Exposure and copy behavior | Current sizing after profits/losses or owner changes; rounding/minimums, copy delays/rejections, and conservative maximum exposure under execution/slippage. |
| Trading schema | Eligibility/settlement, amount-to-notional conversion, quantity precision, limitIOC execution, TP/SL behavior and per-operation outcomes. |
| Recovery and retirement | Provisioning deduplication/discovery, lookup consistency, close/modify recovery, and mirror shutdown/settlement on deletion. |

Keep each trading capability disabled until its relevant gates pass. This does not mean all broker
features remain undocumented: provisioning, token scope names, mirroring, trading routes and order
correlation are documented above. The authenticated demo checks and the single authorized allocation
are recorded below; they do not validate unexercised operations.

## Implementation hold and transition

### Implementation validation on 2026-10-07

The configured demo P&L URL was exercised once as an authorized read-only check after the modular
implementation was completed. It returned HTTP success with a top-level `clientPortfolio` object and
the documented exposure collections, but the response contained no account-ID or portfolio-ID field
at either of the inspected outer levels. No values, credentials, or account data were recorded, and no
mutation or environment fallback was attempted.

This is a verified integration discrepancy with the proposed startup identity check: the configured
P&L response can corroborate read access and provide a strategy snapshot, but it cannot by itself bind
that response to `agent_trading_account_id` or `agent_trading_portfolio_id`. The implemented owner
flow therefore retains the create response's child-token fingerprint, named scopes, Agent Portfolio
UUID and GCID as the immutable issuance binding. A successful P&L read is accepted only after that
owner evidence resolves the environment and identity; it is never promoted into scope evidence by
itself.

The owner demo contract was then validated through the production CLI. A single USD 1,000 Agent
Portfolio allocation returned HTTP 201, one child token with exactly the demo read/write scopes, a
USD 10,000 virtual strategy balance and an owner mirror ID. The owner listing showed exactly that
resource. The broker appended a uniqueness suffix to the requested display name, so callers must
persist the returned name rather than assume the requested string is retained verbatim. No portfolio
deletion was attempted.

Live eligibility uses batched `symbols` or `instrumentIds` and returns nested
`eligibilities[].leverageConfigs[]`; it does not return the flat sizing fields previously proposed.
Current quotes come from `GET /api/v1/market-data/instruments/rates`. The cost response uses
`costs[].value` despite the portal schema describing `costs[].amount`. The v2 open request uses
`transaction` and `orderCurrency`, not `direction` and `currency`. The adapter and fixtures now pin
these verified shapes.

A live BTC preview confirmed a USD 10 minimum at leverage one. A USD 1 trigger can be represented
only as `mit`, which rests but becomes a market order when triggered. `limitIOC` cannot satisfy a
remote resting-limit request: it is immediate-or-cancel and the documented limit rate must remain
within 10% of market.

With explicit user authorization, one USD 10 BTC MIT order was subsequently submitted at a USD 1
trigger. Numeric-order lookup returned status 12 (`PendingTriggeredRate`) with no position
executions. Lookup by the exact UUID supplied as the create request's `x-request-id` returned 404
(`No external operation was found`), despite the documented statement that `referenceId` echoes that
header. Numeric order lookup is therefore verified for this order; request-ID-only unknown-outcome
recovery remains an integration gate and must not blindly retry a mutation.

A separately authorized USD 10 BTC market order then filled in the Agent Portfolio as position
`3614853889`, with USD 9.93 initial exposure and USD 0.09 costs. The owner allocation is 10% of the
strategy virtual balance, so the proportional owner estimate was about USD 1. The owner P&L snapshot
showed no position or mirror after six checks over approximately 30 seconds. This is consistent with,
but does not by itself prove, minimum-size suppression of the copied trade. The strategy fill must be
recorded independently, mirror health remains unresolved, and no further exposure may be admitted
until an actual owner fill or explicit non-copy outcome is reconciled.

After confirming the owner real P&L route rejected the credential with HTTP 403 and only the demo
route was accessible, an explicitly authorized USD 100 BTC demo market order filled as strategy
position `3614856337`. Its actual initial exposure was USD 99.9551259 with USD 0.90 costs. At the 10%
copy ratio this yields approximately USD 9.9955, fractionally below the verified USD 10 BTC minimum.
The owner demo snapshot still showed no BTC position or mirror during the following 30 seconds. Do
not assume nominal strategy amount is sufficient for a minimum-size owner copy; include execution
costs/rounding and an explicit buffer, and never stack or replace exposure without new authorization.

The former `trader_api/__init__.py` and `trader` prototype has now been replaced. The implementation
does not expose `MemoryBroker`, create balances locally, or use JSON persistence. Existing prototype
state was left untouched and is not interpreted as a funded mirror or migrated into broker allocation.

Resolve the remaining operation-specific integration gates before enabling those capabilities. There
is no generic broker-cap-setting API to implement based on this document.

Retain the dbzero architecture specified above. Validate installed memo inheritance, explicit commits, prefix-to-file layout and recovery against local `/src/selltime/AGENTS.md`, [prefix documentation](https://docs.dbzero.io/prefixes), [transactions](https://docs.dbzero.io/transactions) and [atomic operations](https://docs.dbzero.io/api-reference/atomic) before implementation.

## Official references

All eToro links below were consulted on 2026-10-07; documentation evidence is separate from account validation.

[index]: https://api-portal.etoro.com/llms.txt
[auth]: https://api-portal.etoro.com/core/getting-started/authentication
[create-agent]: https://api-portal.etoro.com/api-reference/agent-portfolios/create-agent-portfolio-v2
[list-agent]: https://api-portal.etoro.com/api-reference/agent-portfolios/get-agent-portfolios
[allowed-scopes]: https://api-portal.etoro.com/api-reference/agent-portfolios/get-allowed-scopes-v2
[create-token]: https://api-portal.etoro.com/api-reference/agent-portfolios/create-user-token-v2
[update-token]: https://api-portal.etoro.com/api-reference/agent-portfolios/update-user-token-v2
[delete-agent]: https://api-portal.etoro.com/api-reference/agent-portfolios/delete-agent-portfolio
[open-real]: https://api-portal.etoro.com/api-reference/trading--real/create-an-order
[open-demo]: https://api-portal.etoro.com/api-reference/trading--demo/create-an-order
[open-v3]: https://api-portal.etoro.com/api-reference/trading--real/submit-an-order-for-asynchronous-processing
[lookup]: https://api-portal.etoro.com/api-reference/trading--real/get-order-information-and-position-details
[cancel]: https://api-portal.etoro.com/api-reference/trading--real/cancels-an-order-before-it-is-executed
[close]: https://api-portal.etoro.com/api-reference/trading--real/close-position-by-units
[modify]: https://api-portal.etoro.com/api-reference/trading--real/modify-stop-loss-and-take-profit-settings-on-an-open-position
[pnl]: https://api-portal.etoro.com/api-reference/trading--real/get-account-pnl-and-portfolio-details
[equity]: https://api-portal.etoro.com/core/guides/calculate-equity
[rates]: https://api-portal.etoro.com/core/getting-started/rate-limits
[market-rates]: https://api-portal.etoro.com/api-reference/market-data/get-instrument-market-rates
[demo-lookup]: https://api-portal.etoro.com/api-reference/trading--demo/get-order-information-and-position-details
[demo-cancel]: https://api-portal.etoro.com/api-reference/trading--demo/cancels-an-order-before-it-is-executed
[demo-close]: https://api-portal.etoro.com/api-reference/trading--demo/close-demo-position-by-units
[demo-close-lookup]: https://api-portal.etoro.com/api-reference/trading--demo/get-close-order-information-and-closed-position-details
[demo-cancel-close]: https://api-portal.etoro.com/api-reference/trading--demo/cancel-pending-demo-close-order
[demo-modify]: https://api-portal.etoro.com/api-reference/trading--demo/modify-stop-loss-and-take-profit-settings-on-an-open-position
[demo-history]: https://api-portal.etoro.com/api-reference/trading--demo/list-trading-history
[demo-costs]: https://api-portal.etoro.com/api-reference/trading--demo/get-what-if-trading-cost-breakdown
[demo-eligibility]: https://api-portal.etoro.com/api-reference/trading--demo/check-instrument-trading-eligibility
