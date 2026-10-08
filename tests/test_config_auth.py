from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trader_api.auth import DEMO_READ, REAL_READ, ControlScopeVerifier, resolve_context
from trader_api.config import load_profile
from trader_api.domain import Environment, ScopeEvidence
from trader_api.errors import TraderError

from .conftest import StaticVerifier


def test_profile_is_exact_and_environment_comes_from_verified_scope(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))
    context = resolve_context(profile, runtime["verifier"])  # type: ignore[arg-type]
    assert context.environment.value == "demo"
    assert context.can_read is True
    assert context.can_write is True


def test_mixed_scopes_are_ambiguous(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))
    evidence = ScopeEvidence(
        frozenset({DEMO_READ, REAL_READ}),
        "subject",
        "account",
        "portfolio",
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(TraderError, match="both demo and real") as raised:
        resolve_context(profile, StaticVerifier(evidence))
    assert raised.value.code == "AMBIGUOUS_KEY_ENVIRONMENT"


def test_profile_permissions_are_enforced(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))
    profile.path.chmod(0o644)
    with pytest.raises(TraderError) as raised:
        load_profile(str(runtime["profile"]))
    assert raised.value.code == "CONFIG_PERMISSIONS"


def test_owner_retained_control_record_verifies_exact_token(runtime: dict[str, object]) -> None:
    profile = load_profile(str(runtime["profile"]))
    verifier = ControlScopeVerifier({Environment.DEMO: runtime["root"]})  # type: ignore[dict-item]
    evidence = verifier.verify(api_key=profile.api_key, user_key=profile.user_key)
    assert evidence is not None
    assert evidence.trading_portfolio_id == "portfolio-alpha"
    assert evidence.scopes == frozenset(
        {
            "etoro-public:trade.demo:read",
            "etoro-public:trade.demo:write",
        }
    )
