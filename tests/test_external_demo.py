from __future__ import annotations

import os

import pytest


@pytest.mark.external_demo
def test_full_demo_lifecycle_requires_verified_binding() -> None:
    if os.environ.get("TRADER_RUN_EXTERNAL_DEMO") != "1":
        pytest.skip("blocked: set TRADER_RUN_EXTERNAL_DEMO=1 only with dedicated owner/child binding metadata")
    pytest.fail(
        "blocked: external demo harness still requires authorized owner provisioning and verified mirror attribution"
    )
