from __future__ import annotations

import json
from typing import Any

import pytest

import trader_api.cli as cli


class FakeService:
    environment = "demo"

    def __getattr__(self, name: str) -> Any:
        def method(*args: object, **kwargs: object) -> dict[str, object]:
            return {"method": name, "args": list(args), "kwargs": kwargs}

        return method


@pytest.mark.parametrize(
    "arguments,expected_method",
    [
        (["capabilities"], "capabilities"),
        (["status"], "trader_status"),
        (["portfolio"], "portfolio"),
        (["positions"], "positions"),
        (["orders"], "orders"),
        (["reconcile"], "reconcile"),
        (["verify-audit"], "verify_audit"),
        (["init", "--expected-investment", "2000"], "initialize"),
        (["audit", "--limit", "2"], "audit_events"),
        (["history"], "portfolio_history"),
        (["trends"], "trends"),
        (
            [
                "trade",
                "preview-open",
                "--symbol",
                "AAPL",
                "--side",
                "long",
                "--strategy-notional-usd",
                "10",
            ],
            "preview_open",
        ),
        (["trade", "preview-close", "--position-id", "1"], "preview_close"),
        (
            ["trade", "preview-modify", "--position-id", "1", "--stop-loss-rate", "9"],
            "preview_modify",
        ),
        (["trade", "preview-cancel", "--order-id", "1"], "preview_cancel"),
        (
            ["trade", "submit", "--preview-id", "pv", "--idempotency-key", "key"],
            "submit",
        ),
        (["trade", "status", "--intent-id", "intent"], "intent_status"),
    ],
)
def test_cli_routes_every_public_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected_method: str,
) -> None:
    monkeypatch.setattr(cli, "TraderService", lambda *_args, **_kwargs: FakeService())
    assert cli.main(["--trader", "alpha", *arguments, "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["environment"] == "demo"
    assert output["data"]["method"] == expected_method


def test_cli_version_and_missing_trader(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["version"] == cli.VERSION
    assert cli.main(["status", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "TRADER_MISMATCH"
