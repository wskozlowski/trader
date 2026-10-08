# Agentic trader service

This package implements the isolated trader contract in `designs/trader-api-design.md`. It never
turns a locally entered balance into broker money. An owner administrator first registers a verified
Agent Portfolio binding, child-token scope evidence, owner mirror, and separate accounting baselines.
Strategy fills and owner-mirror fills are reconciled independently.

## Setup

The pinned local dbzero-pro build requires Python 3.13.

```bash
uv sync --all-extras --python /usr/bin/python3.13
chmod 600 .env_demo
.venv/bin/trader --help
.venv/bin/trader-admin --help
```

Profiles are fixed project-root filenames and are never merged with ambient variables. Remove the
obsolete `TRADER_ENV`; only broker/owner-verified scopes establish an environment. The committed
`.env_demo.example` contains supported full demo URLs. Never commit an actual profile.

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
