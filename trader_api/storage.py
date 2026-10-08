from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import InitVar, dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast

import dbzero as db0  # type: ignore[import-untyped]

from .domain import Environment, IntentState, Lifecycle, utc_now
from .errors import TraderError

T = TypeVar("T")


def _prefix_init[T](model: type[T]) -> type[T]:
    """Set the memo prefix before the generated dataclass initializer writes fields."""
    initialize = cast(Callable[..., None], model.__init__)

    @wraps(initialize)
    def initialize_in_prefix(self: T, prefix: str, *args: Any, **kwargs: Any) -> None:
        db0.set_prefix(self, prefix)
        initialize(self, prefix, *args, **kwargs)

    cast(Any, model).__init__ = initialize_in_prefix
    return model


# Dbzero discovers singleton prefixes from the leading set_prefix call in __init__.
# Keep these constructors explicit; ordinary memos use generated dataclass initializers.
@db0.memo(singleton=True)
class TraderState:
    trader_id: str
    environment: str
    initialized: bool
    currency: str
    strategy_initial_cap: str
    owner_initial_cap: str
    strategy_realized: str
    owner_realized: str
    strategy_committed: str
    owner_committed: str
    policy_version: int
    audit_sequence: int
    audit_head: str

    def __init__(self, prefix: str, trader_id: str, environment: str) -> None:
        db0.set_prefix(self, prefix)
        self.trader_id = trader_id
        self.environment = environment
        self.initialized = False
        self.currency = "USD"
        self.strategy_initial_cap = "0.00"
        self.owner_initial_cap = "0.00"
        self.strategy_realized = "0.00"
        self.owner_realized = "0.00"
        self.strategy_committed = "0.00"
        self.owner_committed = "0.00"
        self.policy_version = 1
        self.audit_sequence = 0
        self.audit_head = "0" * 64


@db0.memo(singleton=True)
class PortfolioBinding:
    environment: str
    trader_id: str
    owner_account_id: str
    agent_portfolio_id: str
    agent_portfolio_gcid: str
    agent_trading_account_id: str
    agent_trading_portfolio_id: str
    mirror_id: str
    investment_usd: str
    virtual_balance_usd: str
    lifecycle: str
    binding_version: int
    copy_healthy: bool
    credential_fingerprint: str
    scope_names: list[str]
    verified_at: str

    def __init__(self, prefix: str) -> None:
        db0.set_prefix(self, prefix)
        self.environment = ""
        self.trader_id = ""
        self.owner_account_id = ""
        self.agent_portfolio_id = ""
        self.agent_portfolio_gcid = ""
        self.agent_trading_account_id = ""
        self.agent_trading_portfolio_id = ""
        self.mirror_id = ""
        self.investment_usd = "0.00"
        self.virtual_balance_usd = "0.00"
        self.lifecycle = Lifecycle.UNBOUND.value
        self.binding_version = 0
        self.copy_healthy = False
        self.credential_fingerprint = ""
        self.scope_names: list[str] = []
        self.verified_at = ""


@db0.memo
@_prefix_init
@dataclass(eq=False)
class Preview:
    prefix: InitVar[str]
    operation: str
    params_json: str
    created_at: str
    expires_at: str
    state_fingerprint: str
    binding_version: int
    policy_version: int


@db0.memo
@db0.tag_fields("preview", "request_id")
@_prefix_init
@dataclass(eq=False)
class Intent:
    prefix: InitVar[str]
    preview: Preview
    idempotency_key: str
    operation: str
    params_json: str
    request_id: str
    command_digest: str
    binding_version: int
    policy_version: int
    state: str = field(default=IntentState.COMMITTED.value, kw_only=True)
    created_at: str = field(default_factory=lambda: utc_now().isoformat(), kw_only=True)
    broker_order_id: str = field(default="", kw_only=True)
    broker_position_id: str = field(default="", kw_only=True)
    error_code: str = field(default="", kw_only=True)


@db0.memo
@db0.tag_fields("intent")
@_prefix_init
@dataclass(eq=False)
class Reservation:
    prefix: InitVar[str]
    intent: Intent
    strategy_amount_usd: str
    owner_amount_usd: str
    binding_version: int
    policy_version: int
    state: str = field(default="HELD", kw_only=True)


@db0.memo
@db0.tag_fields("intent", "position_id")
@_prefix_init
@dataclass(eq=False)
class Position:
    prefix: InitVar[str]
    position_id: str
    intent: Intent
    symbol: str
    side: str
    instrument_id: int
    leverage: int
    strategy_notional_usd: str
    units: str
    stop_loss_rate: str = field(default="", kw_only=True)
    take_profit_rate: str = field(default="", kw_only=True)
    state: str = field(default="OPEN", kw_only=True)


@db0.memo
@db0.tag_fields("intent", "order_id")
@_prefix_init
@dataclass(eq=False)
class Order:
    prefix: InitVar[str]
    order_id: str
    intent: Intent
    symbol: str
    state: str


@db0.memo(immutable=True)
@db0.tag_fields("intent", "kind")
@_prefix_init
@dataclass(eq=False)
class AuditEvent:
    prefix: InitVar[str]
    sequence: int
    occurred_at: str
    kind: str
    actor: str
    intent: Intent | None
    source: str
    facts_json: str
    previous_hash: str
    event_hash: str


@db0.memo(immutable=True)
@db0.tag_fields("intent", "domain")
@_prefix_init
@dataclass(eq=False)
class LedgerEntry:
    prefix: InitVar[str]
    domain: str
    kind: str
    amount_usd: str
    intent: Intent | None
    occurred_at: str


@db0.memo
@_prefix_init
@dataclass(eq=False)
class TraderRegistration:
    prefix: InitVar[str]
    trader_hash: str
    trader_id: str
    storage_key: str
    service_credential_hash: str
    authorized: bool = field(default=True, kw_only=True)


@db0.memo
@_prefix_init
@dataclass(eq=False)
class ControlReservation:
    prefix: InitVar[str]
    command_digest: str
    storage_key: str
    request_id: str
    binding_version: int
    state: str = field(default="RESERVED", kw_only=True)
    outcome_json: str = field(default="", kw_only=True)


@db0.memo
@_prefix_init
@dataclass(eq=False)
class ProvisioningIntent:
    prefix: InitVar[str]
    request_key_digest: str
    request_id: str
    trader_hash: str
    portfolio_name: str
    investment_usd: str
    scopes: list[str]
    state: str = field(default="COMMITTED", kw_only=True)
    agent_portfolio_id: str = field(default="", kw_only=True)
    credential_reference: str = field(default="", kw_only=True)
    error_code: str = field(default="", kw_only=True)


@db0.memo(immutable=True)
@_prefix_init
@dataclass(eq=False)
class OwnershipClaim:
    prefix: InitVar[str]
    scoped_key_digest: str
    storage_key: str
    entity_type: str
    broker_id: str


@db0.memo
@_prefix_init
@dataclass(eq=False)
class TokenVerification:
    prefix: InitVar[str]
    credential_fingerprint: str
    scopes: list[str]
    subject_id: str
    trading_account_id: str
    trading_portfolio_id: str
    issued_at: str
    expires_at: str
    source: str
    revoked: bool = field(default=False, kw_only=True)


@db0.memo(singleton=True)
class ControlState:
    audit_sequence: int
    audit_head: str

    def __init__(self, prefix: str) -> None:
        db0.set_prefix(self, prefix)
        self.audit_sequence = 0
        self.audit_head = "0" * 64


@db0.memo(immutable=True)
@_prefix_init
@dataclass(eq=False)
class ControlEvent:
    prefix: InitVar[str]
    sequence: int
    occurred_at: str
    kind: str
    facts_json: str
    previous_hash: str
    event_hash: str


_runtime_lock = threading.RLock()
_runtime_root: Path | None = None


def close_dbzero() -> None:
    global _runtime_root
    with _runtime_lock:
        if _runtime_root is not None:
            db0.close()
            _runtime_root = None


def current_dbzero_root() -> Path | None:
    return _runtime_root


class DbzeroStore:
    """Prefix-scoped persistence. Prefix names are never accepted from callers."""

    def __init__(self, root: Path, environment: Environment) -> None:
        resolved = root.resolve()
        allowed = Path(
            "/dbzero-data/trader-dev" if environment is Environment.DEMO else "/dbzero-data/trader"
        ).resolve()
        if resolved != allowed and (environment is Environment.REAL or allowed not in resolved.parents):
            raise TraderError("STORAGE_INVALID", "dbzero root is outside the environment allowlist")
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = resolved
        self.environment = environment
        self.control_prefix = f"/trader/{environment.value}/control"
        global _runtime_root
        with _runtime_lock:
            if _runtime_root is None:
                db0.init(str(resolved), autocommit=False, async_autocommit=False, file_prealloc=False)
                _runtime_root = resolved
            elif _runtime_root != resolved:
                raise TraderError("STORAGE_INVALID", "a process cannot open two dbzero physical roots")
            self._open(self.control_prefix)

    @staticmethod
    def trader_hash(trader_id: str) -> str:
        return hashlib.sha256(trader_id.encode()).hexdigest()

    def _open(self, prefix: str, mode: str = "rw") -> None:
        opened = {item.name for item in db0.get_prefixes()}
        mutable = {item.name for item in db0.get_mutable_prefixes()}
        if prefix not in opened or (mode == "rw" and prefix not in mutable):
            db0.open(prefix, mode, autocommit=False, restricted=True)

    def register(self, trader_id: str, service_credential: str) -> str:
        trader_hash = self.trader_hash(trader_id)
        existing = self.one(TraderRegistration, f"trader:{trader_hash}", prefix=self.control_prefix)
        credential_hash = hashlib.sha256(service_credential.encode()).hexdigest()
        if existing is not None:
            if existing.trader_id != trader_id or existing.service_credential_hash != credential_hash:
                raise TraderError("TRADER_MISMATCH", "trader registration does not match credentials")
            return str(existing.storage_key)
        storage_key = str(uuid.uuid4())
        item = TraderRegistration(self.control_prefix, trader_hash, trader_id, storage_key, credential_hash)
        db0.tags(item).add([f"trader:{trader_hash}", f"storage:{storage_key}"])
        self.append_control_event("TRADER_REGISTERED", {"trader_hash": trader_hash})
        db0.commit(self.control_prefix)
        return storage_key

    def authenticate(self, trader_id: str, service_credential: str | None) -> str:
        trader_hash = self.trader_hash(trader_id)
        registration = self.one(TraderRegistration, f"trader:{trader_hash}", prefix=self.control_prefix)
        if registration is None or not registration.authorized or service_credential is None:
            raise TraderError("AUTHENTICATION_REQUIRED", "valid local service credentials are required")
        presented = hashlib.sha256(service_credential.encode()).hexdigest()
        if not __import__("hmac").compare_digest(presented, str(registration.service_credential_hash)):
            raise TraderError("AUTHENTICATION_REQUIRED", "valid local service credentials are required")
        if registration.trader_id != trader_id:
            raise TraderError("TRADER_MISMATCH", "authenticated trader identity mismatch")
        return str(registration.storage_key)

    def trader_prefix(self, storage_key: str) -> str:
        try:
            normalized = str(uuid.UUID(storage_key))
        except ValueError as exc:
            raise TraderError("STORAGE_INVALID", "invalid opaque trader storage key") from exc
        prefix = f"/trader/{self.environment.value}/traders/{normalized}"
        self._open(prefix)
        return prefix

    def one(self, model: type[T], tag: Any = None, *, prefix: str) -> T | None:
        self._open(prefix)
        query = db0.find(model, *([tag] if tag is not None else []), prefix=prefix)
        return cast(T | None, next(iter(query), None))

    def all(self, model: type[T], *tags: Any, prefix: str) -> list[T]:
        self._open(prefix)
        return cast(list[T], list(db0.find(model, *tags, prefix=prefix)))

    def tag(self, item: Any, *tags: str) -> None:
        db0.tags(item).add(list(tags))

    def commit(self, prefix: str) -> None:
        db0.commit(prefix)

    def state(self, prefix: str, trader_id: str) -> TraderState:
        self._open(prefix)
        return TraderState(prefix, trader_id, self.environment.value)

    def binding(self, prefix: str) -> PortfolioBinding:
        self._open(prefix)
        return PortfolioBinding(prefix)

    def append_audit(
        self,
        prefix: str,
        state: TraderState,
        *,
        kind: str,
        actor: str,
        intent: Intent | None = None,
        source: str = "trader",
        facts: dict[str, Any] | None = None,
    ) -> AuditEvent:
        safe_facts = json.dumps(facts or {}, sort_keys=True, separators=(",", ":"))
        sequence = int(state.audit_sequence) + 1
        occurred_at = utc_now().isoformat()
        command_digest = intent.command_digest if intent is not None else ""
        body = json.dumps(
            [sequence, occurred_at, kind, actor, command_digest, source, safe_facts, state.audit_head],
            separators=(",", ":"),
        )
        event_hash = hashlib.sha256(body.encode()).hexdigest()
        event = AuditEvent(
            prefix,
            sequence,
            occurred_at,
            kind,
            actor,
            intent,
            source,
            safe_facts,
            str(state.audit_head),
            event_hash,
        )
        self.tag(event, "AUDIT")
        state.audit_sequence = sequence
        state.audit_head = event_hash
        return event

    def verify_audit(self, prefix: str, state: TraderState) -> dict[str, Any]:
        events = sorted(self.all(AuditEvent, "AUDIT", prefix=prefix), key=lambda item: item.sequence)
        previous = "0" * 64
        for expected_sequence, event in enumerate(events, 1):
            body = json.dumps(
                [
                    event.sequence,
                    event.occurred_at,
                    event.kind,
                    event.actor,
                    event.intent.command_digest if event.intent is not None else "",
                    event.source,
                    event.facts_json,
                    event.previous_hash,
                ],
                separators=(",", ":"),
            )
            expected_hash = hashlib.sha256(body.encode()).hexdigest()
            if (
                event.sequence != expected_sequence
                or event.previous_hash != previous
                or event.event_hash != expected_hash
            ):
                return {"valid": False, "checked_events": expected_sequence - 1, "head": previous}
            previous = event.event_hash
        valid = previous == state.audit_head and len(events) == int(state.audit_sequence)
        return {"valid": valid, "checked_events": len(events), "head": previous}

    def assert_prefix_isolation(self) -> None:
        registrations = self.all(TraderRegistration, prefix=self.control_prefix)
        storage_keys = [item.storage_key for item in registrations]
        if len(storage_keys) != len(set(storage_keys)):
            raise TraderError("STORAGE_INVALID", "a trader storage prefix has multiple registrations")
        if self.all(TraderState, prefix=self.control_prefix):
            raise TraderError("STORAGE_INVALID", "trader data exists in the control prefix")

    def reserve_control(
        self,
        *,
        command_digest: str,
        storage_key: str,
        request_id: str,
        binding_version: int,
    ) -> ControlReservation:
        existing = self.one(ControlReservation, f"command:{command_digest}", prefix=self.control_prefix)
        if existing is not None:
            return existing
        reservation = ControlReservation(self.control_prefix, command_digest, storage_key, request_id, binding_version)
        self.tag(reservation, f"command:{command_digest}", f"storage:{storage_key}", "CONTROL_RESERVATION")
        self.append_control_event(
            "CONTROL_RESERVATION_COMMITTED",
            {"command_digest": command_digest, "request_id": request_id},
        )
        self.commit(self.control_prefix)
        return reservation

    def unresolved_control(self, storage_key: str) -> Iterable[ControlReservation]:
        return self.all(ControlReservation, f"storage:{storage_key}", prefix=self.control_prefix)

    def claim_ownership(
        self,
        *,
        environment: str,
        trading_account_id: str,
        trading_portfolio_id: str,
        entity_type: str,
        broker_id: str,
        storage_key: str,
    ) -> None:
        scoped_key = "\x1f".join([environment, trading_account_id, trading_portfolio_id, entity_type, broker_id])
        digest = hashlib.sha256(scoped_key.encode()).hexdigest()
        existing = self.one(OwnershipClaim, f"ownership:{digest}", prefix=self.control_prefix)
        if existing is not None:
            if existing.storage_key != storage_key:
                raise TraderError("BROKER_CAPACITY_UNAVAILABLE", "broker entity is already claimed")
            return
        claim = OwnershipClaim(self.control_prefix, digest, storage_key, entity_type, broker_id)
        self.tag(claim, f"ownership:{digest}", f"storage:{storage_key}", "OWNERSHIP")
        self.append_control_event("OWNERSHIP_CLAIMED", {"scoped_key_digest": digest, "entity_type": entity_type})

    def record_scope_evidence(self, credential_fingerprint: str, evidence: Any) -> None:
        tag = f"credential:{credential_fingerprint}"
        existing = self.one(TokenVerification, tag, prefix=self.control_prefix)
        identity = (
            evidence.subject_id,
            evidence.trading_account_id,
            evidence.trading_portfolio_id,
        )
        if existing is not None:
            if (
                existing.subject_id,
                existing.trading_account_id,
                existing.trading_portfolio_id,
            ) != identity:
                raise TraderError("ACCOUNT_MISMATCH", "credential evidence conflicts with its immutable identity")
            existing.scopes = sorted(evidence.scopes)
            existing.expires_at = "" if evidence.expires_at is None else evidence.expires_at.isoformat()
            existing.revoked = bool(evidence.revoked)
        else:
            existing = TokenVerification(
                self.control_prefix,
                credential_fingerprint,
                sorted(evidence.scopes),
                evidence.subject_id,
                evidence.trading_account_id,
                evidence.trading_portfolio_id,
                evidence.issued_at.isoformat(),
                "" if evidence.expires_at is None else evidence.expires_at.isoformat(),
                evidence.source,
            )
            self.tag(existing, tag, "TOKEN_VERIFICATION")
        self.append_control_event(
            "TOKEN_EVIDENCE_RECORDED",
            {"credential_fingerprint": credential_fingerprint, "revoked": bool(evidence.revoked)},
        )
        self.commit(self.control_prefix)

    def scope_evidence(self, credential_fingerprint: str) -> TokenVerification | None:
        return self.one(
            TokenVerification,
            f"credential:{credential_fingerprint}",
            prefix=self.control_prefix,
        )

    def append_control_event(self, kind: str, facts: dict[str, Any]) -> ControlEvent:
        state = ControlState(self.control_prefix)
        sequence = int(state.audit_sequence) + 1
        occurred_at = utc_now().isoformat()
        facts_json = json.dumps(facts, sort_keys=True, separators=(",", ":"))
        body = json.dumps([sequence, occurred_at, kind, facts_json, state.audit_head], separators=(",", ":"))
        event_hash = hashlib.sha256(body.encode()).hexdigest()
        event = ControlEvent(
            self.control_prefix,
            sequence,
            occurred_at,
            kind,
            facts_json,
            str(state.audit_head),
            event_hash,
        )
        self.tag(event, "CONTROL_AUDIT", f"kind:{kind}")
        state.audit_sequence = sequence
        state.audit_head = event_hash
        return event

    def verify_control_audit(self) -> dict[str, Any]:
        state = ControlState(self.control_prefix)
        events = sorted(
            self.all(ControlEvent, "CONTROL_AUDIT", prefix=self.control_prefix),
            key=lambda item: item.sequence,
        )
        previous = "0" * 64
        for expected_sequence, event in enumerate(events, 1):
            body = json.dumps(
                [
                    event.sequence,
                    event.occurred_at,
                    event.kind,
                    event.facts_json,
                    event.previous_hash,
                ],
                separators=(",", ":"),
            )
            expected_hash = hashlib.sha256(body.encode()).hexdigest()
            if (
                event.sequence != expected_sequence
                or event.previous_hash != previous
                or event.event_hash != expected_hash
            ):
                return {"valid": False, "checked_events": expected_sequence - 1}
            previous = event.event_hash
        return {
            "valid": previous == state.audit_head and len(events) == int(state.audit_sequence),
            "checked_events": len(events),
            "head": previous,
        }

    def backup_prefix(self, prefix: str, destination: Path) -> Path:
        resolved = destination.resolve()
        if self.root not in resolved.parents:
            raise TraderError("STORAGE_INVALID", "backup must remain inside the environment root")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        db0.commit(prefix)
        db0.copy_prefix(str(resolved), prefix=prefix)
        return resolved
