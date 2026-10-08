from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trader_api.auth import DEMO_READ, DEMO_WRITE, fingerprint
from trader_api.domain import ScopeEvidence
from trader_api.errors import TraderError
from trader_api.service import TraderService

from .conftest import FakeBroker, StaticVerifier


def test_two_traders_with_overlapping_symbols_are_physically_isolated(
    runtime: dict[str, object],
) -> None:
    alpha_evidence = runtime["evidence"]
    beta_evidence = ScopeEvidence(
        frozenset({DEMO_READ, DEMO_WRITE}),
        "subject-beta",
        "account-beta",
        "portfolio-beta",
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(hours=1),
    )
    runtime["admin"].register_existing(  # type: ignore[union-attr]
        trader_id="beta",
        service_credential="beta-service",
        evidence=beta_evidence,
        owner_account_id="owner-account",
        agent_portfolio_id="agent-beta",
        agent_portfolio_gcid="gcid-beta",
        mirror_id="mirror-beta",
        investment_usd="2000",
        virtual_balance_usd="10000",
        child_user_key_fingerprint=fingerprint("child-beta"),
    )
    from trader_api.config import load_profile

    alpha_profile = load_profile(str(runtime["profile"])).path
    beta_profile = alpha_profile.parent / ".env_beta"
    beta_profile.write_text(
        alpha_profile.read_text()
        .replace("ETORO_USER_KEY=child-secret", "ETORO_USER_KEY=child-beta")
        .replace(
            "TRADER_SERVICE_CREDENTIAL=service-secret",
            "TRADER_SERVICE_CREDENTIAL=beta-service",
        )
    )
    beta_profile.chmod(0o600)
    alpha_broker = FakeBroker()
    beta_broker = FakeBroker()
    alpha = TraderService(
        "alpha",
        str(runtime["profile"]),
        scope_verifier=StaticVerifier(alpha_evidence),  # type: ignore[arg-type]
        broker=alpha_broker,
        storage_root=runtime["root"],  # type: ignore[arg-type]
    )
    beta = TraderService(
        "beta",
        ".env_beta",
        scope_verifier=StaticVerifier(beta_evidence),
        broker=beta_broker,
        storage_root=runtime["root"],  # type: ignore[arg-type]
    )
    alpha.initialize("2000")
    beta.initialize("2000")
    alpha_preview = alpha.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    alpha.submit(alpha_preview["preview_id"], "shared-looking-key")
    assert beta.positions() == []
    with pytest.raises(TraderError) as foreign_preview:
        beta.submit(alpha_preview["preview_id"], "shared-looking-key")
    assert foreign_preview.value.code == "NOT_FOUND"
    beta_preview = beta.preview_open(symbol="AAPL", side="long", strategy_notional_usd="100")
    beta.submit(beta_preview["preview_id"], "shared-looking-key")
    assert alpha.positions()[0]["symbol"] == beta.positions()[0]["symbol"] == "AAPL"
    assert len(alpha.audit_events()) == len(beta.audit_events())


def test_service_credential_cannot_be_rebound_to_another_trader(
    runtime: dict[str, object],
) -> None:
    with pytest.raises(TraderError) as denied:
        TraderService(
            "not-alpha",
            str(runtime["profile"]),
            scope_verifier=runtime["verifier"],  # type: ignore[arg-type]
            broker=runtime["broker"],  # type: ignore[arg-type]
            storage_root=runtime["root"],  # type: ignore[arg-type]
        )
    assert denied.value.code == "AUTHENTICATION_REQUIRED"
