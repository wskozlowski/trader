from __future__ import annotations

import json

from trader_api.broker.transport import redact
from trader_api.cli import main


def test_recursive_redaction() -> None:
    value = redact(
        {"x-api-key": "secret", "message": "failed with secret", "nested": [{"token": "abc"}]},
        ("secret",),
    )
    assert value == {
        "x-api-key": "<redacted>",
        "message": "failed with <redacted>",
        "nested": [{"token": "<redacted>"}],
    }


def test_cli_rejects_prototype_arguments(capsys: object) -> None:
    assert main(["--env", "demo", "--trader", "alpha", "status", "--json"]) == 1
    output = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
    assert output["error"]["code"] == "OBSOLETE_ARGUMENT"
    assert "expected-environment" in output["error"]["message"]
