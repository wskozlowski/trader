from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

import trader_api.admin_cli as admin_cli
import trader_api.config as config_module
from trader_api.auth import DEMO_READ, DEMO_WRITE
from trader_api.broker.owner import EtoroOwnerBroker
from trader_api.config import load_profile
from trader_api.errors import TraderError
from trader_api.provisioning import ProvisionedPortfolio


def _created_payload() -> dict[str, object]:
    return {
        "agentPortfolioId": "11111111-1111-4111-8111-111111111111",
        "agentPortfolioName": "demo-name-X1",
        "agentPortfolioGcid": 12345,
        "agentPortfolioVirtualBalance": 10000,
        "mirrorId": 67890,
        "userTokens": [
            {
                "userToken": "one-time-child-secret",
                "scopes": [{"name": DEMO_READ}, {"name": DEMO_WRITE}],
            }
        ],
    }


def test_owner_adapter_create_and_list_contract(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(201, json=_created_payload())
        return httpx.Response(
            200,
            json={
                "agentPortfolios": [
                    {
                        "agentPortfolioId": "11111111-1111-4111-8111-111111111111",
                        "agentPortfolioName": "demo-name-X1",
                        "agentPortfolioGcid": 12345,
                        "agentPortfolioVirtualBalance": 10000,
                        "mirrorId": 67890,
                        "userTokens": [{"scopeNames": [DEMO_READ, DEMO_WRITE]}],
                    }
                ]
            },
        )

    broker = EtoroOwnerBroker(profile, httpx.Client(transport=httpx.MockTransport(handler)))
    created = broker.create_portfolio(
        request_id="11111111-1111-4111-8111-111111111112",
        investment_usd=Decimal("1000"),
        name="demo-name",
        token_name="demo-token",
        scopes=frozenset({DEMO_READ, DEMO_WRITE}),
    )
    assert created.investment_usd == Decimal("1000.00")
    assert created.child_user_key == "one-time-child-secret"
    assert created.agent_trading_account_id == "12345"
    body = json.loads(calls[0].content)
    assert body["investmentAmountInUsd"] == "1000"
    assert body["scopeNames"] == sorted([DEMO_READ, DEMO_WRITE])
    listed = broker.list_portfolios()
    assert listed[0].portfolio_name == "demo-name-X1"
    assert listed[0].child_user_key == ""


@pytest.mark.parametrize(
    ("status", "code"),
    [(207, "BROKER_OUTCOME_UNKNOWN"), (400, "BROKER_REJECTED"), (500, "BROKER_OUTCOME_UNKNOWN")],
)
def test_owner_adapter_never_retries_non_success(runtime: dict[str, object], status: int, code: str) -> None:
    profile = load_profile(str(runtime["profile"]))
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(status, json={})))
    broker = EtoroOwnerBroker(profile, client)
    with pytest.raises(TraderError) as raised:
        broker.create_portfolio(
            request_id="11111111-1111-4111-8111-111111111112",
            investment_usd=Decimal("1000"),
            name="demo-name",
            token_name="demo-token",
            scopes=frozenset({DEMO_READ, DEMO_WRITE}),
        )
    assert raised.value.code == code


class CliOwnerBroker:
    def __init__(self, profile: object) -> None:
        self.owner_identity = "owner-fixture"

    def create_portfolio(self, **kwargs: Any) -> ProvisionedPortfolio:
        return ProvisionedPortfolio(
            "owner-fixture",
            "11111111-1111-4111-8111-111111111111",
            "12345",
            "12345",
            "11111111-1111-4111-8111-111111111111",
            "67890",
            Decimal("1000.00"),
            Decimal("10000.00"),
            "one-time-child-secret",
            frozenset({DEMO_READ, DEMO_WRITE}),
            "demo-name-X1",
        )

    def list_portfolios(self) -> list[ProvisionedPortfolio]:
        return []


def test_admin_cli_bootstrap_and_provision(
    runtime: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project_root = config_module.PROJECT_ROOT
    profile_path = project_root / str(runtime["profile"])
    monkeypatch.setattr(admin_cli, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(admin_cli, "fixed_storage_root", lambda _environment: runtime["root"])
    monkeypatch.setattr(admin_cli, "EtoroOwnerBroker", CliOwnerBroker)

    assert admin_cli.main(["--config", profile_path.name, "bootstrap"]) == 0
    bootstrap = json.loads(capsys.readouterr().out)
    assert bootstrap["data"]["generated_secret_count"] == 2
    assert admin_cli.main(["--config", profile_path.name, "bootstrap"]) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["data"]["generated_secret_count"] == 0

    exit_code = admin_cli.main(
        [
            "--config",
            profile_path.name,
            "provision",
            "--trader",
            "cli-owner-test",
            "--investment-usd",
            "1000",
            "--portfolio-name",
            "demo-name",
            "--token-name",
            "demo-token",
            "--administrative-request-key",
            "demo-admin-key",
            "--child-config",
            ".env_child_test",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["data"]["state"] == "READY"
    child = project_root / ".env_child_test"
    assert child.stat().st_mode & 0o777 == 0o600
    assert "one-time-child-secret" in child.read_text()
    assert "one-time-child-secret" not in json.dumps(output)
