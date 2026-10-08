from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .config import Profile, route_environment
from .domain import Environment, ScopeEvidence, VerifiedContext, utc_now
from .errors import TraderError

DEMO_READ = "etoro-public:trade.demo:read"
DEMO_WRITE = "etoro-public:trade.demo:write"
REAL_READ = "etoro-public:trade.real:read"
REAL_WRITE = "etoro-public:trade.real:write"


class ScopeVerifier(Protocol):
    """Obtains broker/owner evidence bound to the exact opaque user key."""

    def verify(self, *, api_key: str, user_key: str) -> ScopeEvidence | None: ...


class UnavailableScopeVerifier:
    def verify(self, *, api_key: str, user_key: str) -> ScopeEvidence | None:
        return None


class ControlScopeVerifier:
    """Reads only owner-retained issuance evidence; it never derives claims from the key."""

    def __init__(self, roots: dict[Environment, Path] | None = None) -> None:
        self.roots = roots

    def verify(self, *, api_key: str, user_key: str) -> ScopeEvidence | None:
        from .config import fixed_storage_root
        from .storage import DbzeroStore, close_dbzero, current_dbzero_root

        records: list[ScopeEvidence] = []
        roots = self.roots or {environment: fixed_storage_root(environment) for environment in Environment}
        credential_fingerprint = fingerprint(user_key)
        for environment, root in roots.items():
            if not root.exists():
                continue
            previously_open = current_dbzero_root()
            if previously_open is not None and previously_open != root.resolve():
                continue
            try:
                store = DbzeroStore(root, environment)
                record = store.scope_evidence(credential_fingerprint)
                if record is not None:
                    records.append(
                        ScopeEvidence(
                            scopes=frozenset(record.scopes),
                            subject_id=str(record.subject_id),
                            trading_account_id=str(record.trading_account_id),
                            trading_portfolio_id=str(record.trading_portfolio_id),
                            issued_at=datetime.fromisoformat(record.issued_at),
                            expires_at=(None if not record.expires_at else datetime.fromisoformat(record.expires_at)),
                            revoked=bool(record.revoked),
                            source=str(record.source),
                        )
                    )
            finally:
                if previously_open is None:
                    close_dbzero()
        if not records:
            return None
        if len(records) == 1:
            return records[0]
        first = records[0]
        return ScopeEvidence(
            scopes=frozenset().union(*(record.scopes for record in records)),
            subject_id=first.subject_id,
            trading_account_id=first.trading_account_id,
            trading_portfolio_id=first.trading_portfolio_id,
            issued_at=max(record.issued_at for record in records),
            revoked=any(record.revoked for record in records),
            source="multiple_control_records",
        )


def fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16]


def resolve_context(
    profile: Profile,
    verifier: ScopeVerifier,
    *,
    expected_environment: str | Environment | None = None,
    now: datetime | None = None,
) -> VerifiedContext:
    evidence = verifier.verify(api_key=profile.api_key, user_key=profile.user_key)
    if evidence is None:
        raise TraderError(
            "ENVIRONMENT_UNVERIFIED",
            "the user token has no broker-verified scope record",
        )
    current = now or utc_now()
    if evidence.revoked or (evidence.expires_at is not None and evidence.expires_at <= current):
        raise TraderError("PERMISSION_REVOKED", "the verified user token is revoked or expired")
    demo = bool(evidence.scopes & {DEMO_READ, DEMO_WRITE})
    real = bool(evidence.scopes & {REAL_READ, REAL_WRITE})
    if demo and real:
        raise TraderError("AMBIGUOUS_KEY_ENVIRONMENT", "the user token has both demo and real scopes")
    if not demo and not real:
        raise TraderError("ENVIRONMENT_UNVERIFIED", "the user token has no trading environment scope")
    environment = Environment.DEMO if demo else Environment.REAL
    if expected_environment is not None and Environment(expected_environment) is not environment:
        raise TraderError("ENVIRONMENT_ASSERTION_MISMATCH", "expected environment does not match verified token scopes")
    configured = route_environment(profile)
    if configured is not None and configured is not environment:
        raise TraderError("ENDPOINT_ENVIRONMENT_MISMATCH", "operation URLs do not match verified token scopes")
    read_scope = DEMO_READ if environment is Environment.DEMO else REAL_READ
    write_scope = DEMO_WRITE if environment is Environment.DEMO else REAL_WRITE
    return VerifiedContext(
        environment=environment,
        subject_id=evidence.subject_id,
        trading_account_id=evidence.trading_account_id,
        trading_portfolio_id=evidence.trading_portfolio_id,
        credential_fingerprint=fingerprint(profile.user_key),
        scopes=evidence.scopes,
        verified_at=current,
        can_read=read_scope in evidence.scopes,
        can_write=write_scope in evidence.scopes,
    )


def verify_service_credential(presented: str | None, expected: str | None) -> None:
    if expected is None or presented is None or not hmac.compare_digest(presented, expected):
        raise TraderError("AUTHENTICATION_REQUIRED", "valid local service credentials are required")
