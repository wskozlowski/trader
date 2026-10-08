from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from trader_api.auth import DEMO_READ, DEMO_WRITE
from trader_api.errors import TraderError
from trader_api.provisioning import ProvisionedPortfolio
from trader_api.secrets import CredentialVault


class OwnerBrokerFixture:
    def __init__(self) -> None:
        self.calls = 0
        self.created = ProvisionedPortfolio(
            "owner",
            "agent-beta",
            "gcid-beta",
            "account-beta",
            "portfolio-beta",
            "mirror-beta",
            Decimal("2000.00"),
            Decimal("10000.00"),
            "child-beta-secret",
            frozenset({DEMO_READ, DEMO_WRITE}),
            "test-beta",
        )

    def create_portfolio(self, **kwargs: object) -> ProvisionedPortfolio:
        self.calls += 1
        return self.created

    def list_portfolios(self) -> list[ProvisionedPortfolio]:
        return [self.created]


def test_owner_provisioning_is_durable_idempotent_and_encrypts_token(
    runtime: dict[str, object], tmp_path: Path
) -> None:
    broker = OwnerBrokerFixture()
    vault = CredentialVault(tmp_path / "vault", "a-sufficiently-long-owner-master-key")
    admin = runtime["admin"]
    result = admin.provision(  # type: ignore[union-attr]
        trader_id="beta",
        service_credential="beta-service",
        investment_usd="2000",
        portfolio_name="test-beta",
        token_name="test-beta-token",
        administrative_request_key="provision-beta",
        broker=broker,
        vault=vault,
    )
    assert result["state"] == "READY"
    assert str(result["credential_reference"]).startswith("vault:")
    repeated = admin.provision(  # type: ignore[union-attr]
        trader_id="beta",
        service_credential="beta-service",
        investment_usd="2000",
        portfolio_name="test-beta",
        token_name="test-beta-token",
        administrative_request_key="provision-beta",
        broker=broker,
        vault=vault,
    )
    assert repeated["state"] == "READY"
    assert broker.calls == 1
    assert admin.verify_control_audit()["valid"] is True  # type: ignore[union-attr]


def test_ambiguous_provisioning_is_not_retried(runtime: dict[str, object], tmp_path: Path) -> None:
    broker = OwnerBrokerFixture()

    def unknown(**kwargs: object) -> ProvisionedPortfolio:
        broker.calls += 1
        raise TraderError("BROKER_OUTCOME_UNKNOWN", "timeout")

    broker.create_portfolio = unknown  # type: ignore[method-assign]
    vault = CredentialVault(tmp_path / "vault", "a-sufficiently-long-owner-master-key")
    admin = runtime["admin"]
    first = admin.provision(  # type: ignore[union-attr]
        trader_id="gamma",
        service_credential="gamma-service",
        investment_usd="2000",
        portfolio_name="test-gamma",
        token_name="test-gamma-token",
        administrative_request_key="provision-gamma",
        broker=broker,
        vault=vault,
    )
    second = admin.provision(  # type: ignore[union-attr]
        trader_id="gamma",
        service_credential="gamma-service",
        investment_usd="2000",
        portfolio_name="test-gamma",
        token_name="test-gamma-token",
        administrative_request_key="provision-gamma",
        broker=broker,
        vault=vault,
    )
    assert first["state"] == second["state"] == "UNKNOWN"
    assert broker.calls == 1
    inspection = admin.inspect_unknown_provisioning(  # type: ignore[union-attr]
        administrative_request_key="provision-gamma", broker=broker
    )
    assert inspection["state"] == "UNKNOWN"
