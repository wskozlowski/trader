from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from .auth import DEMO_READ, DEMO_WRITE, REAL_READ, REAL_WRITE, fingerprint
from .config import fixed_storage_root
from .domain import Environment, Lifecycle, ScopeEvidence, money, utc_now
from .errors import TraderError
from .secrets import CredentialVault
from .storage import (
    DbzeroStore,
    LedgerEntry,
    ProvisioningIntent,
    TraderRegistration,
)


@dataclass(frozen=True, slots=True)
class ProvisionedPortfolio:
    owner_account_id: str
    agent_portfolio_id: str
    agent_portfolio_gcid: str
    agent_trading_account_id: str
    agent_trading_portfolio_id: str
    mirror_id: str
    investment_usd: Decimal
    virtual_balance_usd: Decimal
    child_user_key: str
    scopes: frozenset[str]
    portfolio_name: str = ""


class OwnerBroker(Protocol):
    def create_portfolio(
        self,
        *,
        request_id: str,
        investment_usd: Decimal,
        name: str,
        token_name: str,
        scopes: frozenset[str],
    ) -> ProvisionedPortfolio: ...

    def list_portfolios(self) -> list[ProvisionedPortfolio]: ...


class OwnerAdminService:
    """Separate administrative surface; never accepts a child trading credential."""

    def __init__(
        self,
        *,
        environment: Environment,
        owner_identity: str,
        owner_service_credential: str,
        expected_owner_service_credential: str,
        storage_root: Path | None = None,
    ) -> None:
        import hmac

        if not hmac.compare_digest(owner_service_credential, expected_owner_service_credential):
            raise TraderError("AUTHENTICATION_REQUIRED", "owner administrator authentication failed")
        self.environment = environment
        self.owner_identity = owner_identity
        self.store = DbzeroStore(storage_root or fixed_storage_root(environment), environment)

    def provision(
        self,
        *,
        trader_id: str,
        service_credential: str,
        investment_usd: object,
        portfolio_name: str,
        token_name: str,
        administrative_request_key: str,
        broker: OwnerBroker,
        vault: CredentialVault,
    ) -> dict[str, object]:
        """Durably provision once; an ambiguous outcome is never blindly resubmitted."""
        investment = money(investment_usd)
        if investment <= 0 or not administrative_request_key or not portfolio_name or not token_name:
            raise TraderError("INVALID_PROVISIONING_REQUEST", "complete provisioning inputs are required")
        key_digest = hashlib.sha256(administrative_request_key.encode()).hexdigest()
        key_tag = f"provision-key:{key_digest}"
        existing = self.store.one(ProvisioningIntent, key_tag, prefix=self.store.control_prefix)
        if existing is not None:
            return {
                "request_id": existing.request_id,
                "state": existing.state,
                "agent_portfolio_id": existing.agent_portfolio_id or None,
                "error_code": existing.error_code or None,
            }
        scopes = (
            frozenset({DEMO_READ, DEMO_WRITE})
            if self.environment is Environment.DEMO
            else frozenset({REAL_READ, REAL_WRITE})
        )
        request_id = str(uuid.uuid4())
        self.store.open(self.store.control_prefix)
        intent = ProvisioningIntent(
            key_digest,
            request_id,
            self.store.trader_hash(trader_id),
            portfolio_name,
            str(investment),
            sorted(scopes),
        )
        self.store.tag(intent, key_tag, f"provision-request:{request_id}", "PROVISIONING")
        self.store.append_control_event(
            "PROVISIONING_COMMITTED", {"request_id": request_id, "trader_hash": intent.trader_hash}
        )
        self.store.commit(self.store.control_prefix)
        try:
            created = broker.create_portfolio(
                request_id=request_id,
                investment_usd=investment,
                name=portfolio_name,
                token_name=token_name,
                scopes=scopes,
            )
        except TraderError as exc:
            intent.state = "UNKNOWN" if exc.code == "BROKER_OUTCOME_UNKNOWN" else "REJECTED"
            intent.error_code = exc.code
            self.store.commit(self.store.control_prefix)
            return {
                "request_id": request_id,
                "state": intent.state,
                "agent_portfolio_id": None,
                "error_code": intent.error_code,
            }
        if created.investment_usd != investment or created.scopes != scopes:
            intent.state = "REPAIR_REQUIRED"
            intent.error_code = "PORTFOLIO_SCOPE_MISMATCH"
            self.store.commit(self.store.control_prefix)
            raise TraderError(
                "PORTFOLIO_SCOPE_MISMATCH",
                "created portfolio does not match the committed investment and least-privilege scopes",
            )
        secret_reference = vault.put(f"agent-portfolio:{created.agent_portfolio_id}", created.child_user_key)
        intent.agent_portfolio_id = created.agent_portfolio_id
        intent.credential_reference = secret_reference
        intent.state = "SECRET_PERSISTED"
        self.store.commit(self.store.control_prefix)
        evidence = ScopeEvidence(
            scopes=created.scopes,
            subject_id=created.agent_portfolio_gcid,
            trading_account_id=created.agent_trading_account_id,
            trading_portfolio_id=created.agent_trading_portfolio_id,
            issued_at=utc_now(),
        )
        self.register_existing(
            trader_id=trader_id,
            service_credential=service_credential,
            evidence=evidence,
            owner_account_id=created.owner_account_id,
            agent_portfolio_id=created.agent_portfolio_id,
            agent_portfolio_gcid=created.agent_portfolio_gcid,
            mirror_id=created.mirror_id,
            investment_usd=created.investment_usd,
            virtual_balance_usd=created.virtual_balance_usd,
            child_user_key_fingerprint=fingerprint(created.child_user_key),
        )
        intent.state = "READY"
        self.store.append_control_event(
            "PROVISIONING_READY",
            {"request_id": request_id, "agent_portfolio_id": created.agent_portfolio_id},
        )
        self.store.commit(self.store.control_prefix)
        return {
            "request_id": request_id,
            "state": intent.state,
            "agent_portfolio_id": created.agent_portfolio_id,
            "credential_reference": secret_reference,
        }

    def inspect_unknown_provisioning(
        self, *, administrative_request_key: str, broker: OwnerBroker
    ) -> dict[str, object]:
        key_digest = hashlib.sha256(administrative_request_key.encode()).hexdigest()
        intent = self.store.one(
            ProvisioningIntent,
            f"provision-key:{key_digest}",
            prefix=self.store.control_prefix,
        )
        if intent is None:
            raise TraderError("NOT_FOUND", "provisioning request not found")
        if intent.state != "UNKNOWN":
            return {"request_id": intent.request_id, "state": intent.state}
        candidates = [
            item
            for item in broker.list_portfolios()
            if item.portfolio_name == intent.portfolio_name
            or (not item.portfolio_name and item.investment_usd == money(intent.investment_usd))
        ]
        return {
            "request_id": intent.request_id,
            "state": "OWNER_ACTION_REQUIRED" if candidates else "UNKNOWN",
            "discovered_candidates": len(candidates),
            "guidance": "replace the child token under owner authorization; do not create another funded portfolio",
        }

    def register_existing(
        self,
        *,
        trader_id: str,
        service_credential: str,
        evidence: ScopeEvidence,
        owner_account_id: str,
        agent_portfolio_id: str,
        agent_portfolio_gcid: str,
        mirror_id: str,
        investment_usd: object,
        virtual_balance_usd: object,
        child_user_key_fingerprint: str,
    ) -> dict[str, object]:
        if not evidence.trading_account_id or not evidence.trading_portfolio_id:
            raise TraderError("PORTFOLIO_NOT_READY", "child token identity metadata is incomplete")
        investment = money(investment_usd)
        virtual_balance = money(virtual_balance_usd)
        if investment <= 0 or virtual_balance <= 0:
            raise TraderError("INVALID_AMOUNT", "investment and virtual balance must be positive")
        storage_key = self.store.register(trader_id, service_credential)
        prefix = self.store.trader_prefix(storage_key)
        state = self.store.state(prefix, trader_id)
        binding = self.store.binding(prefix)
        if binding.binding_version and binding.agent_portfolio_id != agent_portfolio_id:
            raise TraderError("ALREADY_INITIALIZED", "trader already has a different immutable binding")
        binding.environment = self.environment.value
        binding.trader_id = trader_id
        binding.owner_account_id = owner_account_id
        binding.agent_portfolio_id = agent_portfolio_id
        binding.agent_portfolio_gcid = agent_portfolio_gcid
        binding.agent_trading_account_id = evidence.trading_account_id
        binding.agent_trading_portfolio_id = evidence.trading_portfolio_id
        binding.mirror_id = mirror_id
        binding.investment_usd = str(investment)
        binding.virtual_balance_usd = str(virtual_balance)
        binding.lifecycle = Lifecycle.READY.value
        binding.binding_version = max(1, int(binding.binding_version))
        binding.copy_healthy = True
        binding.credential_fingerprint = child_user_key_fingerprint
        binding.scope_names = sorted(evidence.scopes)
        binding.verified_at = utc_now().isoformat()
        self.store.record_scope_evidence(child_user_key_fingerprint, evidence)
        self.store.append_audit(
            prefix,
            state,
            kind="BINDING_VERIFIED",
            actor="owner-admin",
            facts={
                "agent_portfolio_id": agent_portfolio_id,
                "investment_usd": str(investment),
                "virtual_balance_usd": str(virtual_balance),
                "credential_fingerprint": child_user_key_fingerprint,
            },
        )
        self.store.commit(prefix)
        return {"trader_id": trader_id, "lifecycle": binding.lifecycle, "binding_version": binding.binding_version}

    def set_suspended(self, *, trader_id: str, suspended: bool) -> dict[str, object]:
        registration = self.store.one(
            TraderRegistration,
            f"trader:{self.store.trader_hash(trader_id)}",
            prefix=self.store.control_prefix,
        )
        if registration is None:
            raise TraderError("NOT_FOUND", "trader not found")
        prefix = self.store.trader_prefix(str(registration.storage_key))
        state = self.store.state(prefix, trader_id)
        binding = self.store.binding(prefix)
        if binding.lifecycle == Lifecycle.RETIRED.value:
            raise TraderError("PORTFOLIO_SUSPENDED", "retired portfolio cannot be reactivated")
        binding.lifecycle = (
            Lifecycle.SUSPENDED.value
            if suspended
            else (Lifecycle.ACTIVE.value if state.initialized else Lifecycle.READY.value)
        )
        self.store.append_audit(
            prefix,
            state,
            kind="SUSPENDED" if suspended else "RESUMED",
            actor="owner-admin",
        )
        self.store.commit(prefix)
        return {"trader_id": trader_id, "lifecycle": binding.lifecycle}

    def verify_control_audit(self) -> dict[str, object]:
        return self.store.verify_control_audit()

    def record_mirror_reconciliation(
        self,
        *,
        trader_id: str,
        actual_realized_pnl_usd: object,
        actual_committed_usd: object,
        copy_healthy: bool,
    ) -> dict[str, object]:
        registration = self.store.one(
            TraderRegistration,
            f"trader:{self.store.trader_hash(trader_id)}",
            prefix=self.store.control_prefix,
        )
        if registration is None:
            raise TraderError("NOT_FOUND", "trader not found")
        prefix = self.store.trader_prefix(str(registration.storage_key))
        state = self.store.state(prefix, trader_id)
        binding = self.store.binding(prefix)
        realized = money(actual_realized_pnl_usd, allow_negative=True)
        committed = money(actual_committed_usd)
        state.owner_realized = str(realized)
        state.owner_committed = str(committed)
        binding.copy_healthy = bool(copy_healthy)
        self.store.open(prefix)
        entry = LedgerEntry(
            "owner_mirror",
            "RECONCILED_ACTUAL",
            str(realized),
            None,
            utc_now().isoformat(),
        )
        self.store.tag(entry, "LEDGER")
        self.store.append_audit(prefix, state, kind="MIRROR_RECONCILED", actor="owner-admin", source="reconcile")
        self.store.commit(prefix)
        return {"copy_healthy": binding.copy_healthy, "actual_committed_usd": str(committed)}
