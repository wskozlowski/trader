"""Small, safety-first trader service implementation.

The broker is deliberately injected.  ``MemoryBroker`` is used by tests and
never represents a real account; production callers must provide a verified
official demo/real adapter.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path
from threading import RLock
from typing import Any, Dict, Optional


class TraderError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def money(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TraderError("INVALID_AMOUNT", "amount must be a decimal")
    if not d.is_finite() or d < 0:
        raise TraderError("INVALID_AMOUNT", "amount must be finite and non-negative")
    return d.quantize(Decimal("0.01"))

def signed_money(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TraderError("INVALID_AMOUNT", "amount must be a decimal")
    if not d.is_finite(): raise TraderError("INVALID_AMOUNT", "amount must be finite")
    return d.quantize(Decimal("0.01"))


@dataclass
class Position:
    id: str
    symbol: str
    side: str
    notional: Decimal
    leverage: int


@dataclass
class Preview:
    preview_id: str
    operation: str
    created_at: datetime
    expires_at: datetime
    params: Dict[str, Any]
    estimated_costs: Decimal
    remaining_budget: Decimal
    fingerprint: str


class Broker:
    """Adapter contract. Implementations must use official, verified APIs."""
    def capabilities(self) -> dict: return {"market": False}
    def state_fingerprint(self) -> str: raise NotImplementedError
    def open(self, **kwargs): raise NotImplementedError
    def close(self, **kwargs): raise NotImplementedError


class MemoryBroker(Broker):
    """Deterministic broker used for tests and local development only."""
    def __init__(self):
        self.positions: dict[str, dict] = {}
        self._counter = 0
        self.lock = RLock()

    def capabilities(self):
        return {"market": True, "limit": False, "stop": False, "tp_sl": True, "partial_close": True}

    def state_fingerprint(self):
        return hashlib.sha256(json.dumps(self.positions, sort_keys=True, default=str).encode()).hexdigest()

    def open(self, **kwargs):
        with self.lock:
            self._counter += 1
            pid = f"pos_{self._counter}"
            item = dict(kwargs)
            if "notional" in item: item["notional"] = Decimal(str(item["notional"]))
            self.positions[pid] = item
            return pid

    def close(self, position_id, fraction=Decimal("1"), **_):
        with self.lock:
            if position_id not in self.positions:
                raise TraderError("NOT_FOUND", "position not found")
            p = self.positions[position_id]
            fraction = Decimal(str(fraction))
            released = p["notional"] * fraction
            if fraction >= 1:
                del self.positions[position_id]
            else:
                p["notional"] -= released
            return {"released": released, "realized_pnl": Decimal("0")}


class TraderService:
    """Trader-local API. State is isolated by ``(environment, trader_id)``."""
    def __init__(self, authenticated_identity: str, environment: str = "demo",
                 broker: Optional[Broker] = None, state_root: Optional[str] = None):
        if environment not in ("demo", "real"):
            raise TraderError("INVALID_ENVIRONMENT", "environment must be demo or real")
        if not authenticated_identity:
            raise TraderError("TRADER_MISMATCH", "authenticated identity is required")
        self.trader_id, self.environment = authenticated_identity, environment
        self.broker = broker or self._configured_broker(environment)
        root = Path(state_root or os.environ.get("TRADER_STATE_ROOT", ".trader-state"))
        self._dir = root / environment / hashlib.sha256(authenticated_identity.encode()).hexdigest()[:24]
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "state.json"
        self._lock = RLock()
        self._state = self._load()
        self._previews: dict[str, Preview] = {}

    @staticmethod
    def _configured_broker(environment):
        # Fail closed: dotenv is never merged and credentials are never accepted
        # as a substitute for a verified official adapter.
        filename = ".env_demo" if environment == "demo" else ".env"
        path = Path(__file__).resolve().parent / filename
        if not path.exists():
            raise TraderError("CONFIG_INVALID", f"{filename} is missing")
        values = {}
        for line in path.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"): continue
            if "=" not in line or line.split("=", 1)[0] in values:
                raise TraderError("CONFIG_INVALID", f"invalid {filename}")
            k, v = line.split("=", 1); values[k] = v
        if values.get("TRADER_ENV") != environment or not values.get("ETORO_API_URL"):
            raise TraderError("CONFIG_INVALID", f"{filename} is not a verified {environment} configuration")
        raise TraderError("BROKER_UNAVAILABLE", "no verified official broker adapter is configured")

    def _load(self):
        if not self._file.exists(): return {"initialized": False, "positions": [], "orders": [], "realized": "0", "committed": "0", "keys": {}}
        raw = json.loads(self._file.read_text())
        raw.setdefault("positions", []); raw.setdefault("orders", []); raw.setdefault("keys", {})
        return raw

    def _save(self):
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, sort_keys=True))
        tmp.replace(self._file)

    def initialize(self, balance, currency="USD"):
        balance = money(balance)
        if not currency or len(currency) != 3: raise TraderError("INVALID_CURRENCY", "currency must be ISO-4217")
        with self._lock:
            if self._state.get("initialized"):
                if money(self._state["initial_balance"]) == balance and self._state["currency"] == currency:
                    return self.trader_status()
                raise TraderError("ALREADY_INITIALIZED", "trader is already initialized")
            self._state.update(initialized=True, initial_balance=str(balance), currency=currency, realized="0", committed="0")
            self._save(); return self.trader_status()

    def _require_init(self):
        if not self._state.get("initialized"): raise TraderError("NOT_INITIALIZED", "initialize trader first")

    def trader_status(self):
        self._require_init()
        realized = signed_money(self._state.get("realized", "0"))
        ceiling = money(self._state["initial_balance"]) + realized
        committed = money(self._state.get("committed", "0"))
        return {"trader_id": self.trader_id, "environment": self.environment, "currency": self._state["currency"], "initial_balance": str(money(self._state["initial_balance"])), "settled_realized_pnl": str(realized), "committed": str(committed), "available_to_open": str(max(Decimal("0"), ceiling - committed))}

    def capabilities(self): return {"environment": self.environment, "broker": self.broker.capabilities()}
    def positions(self): self._require_init(); return list(self._state["positions"])
    def orders(self): self._require_init(); return list(self._state["orders"])

    def preview_open(self, *, symbol, side, order_type="market", notional, leverage=1, stop_loss_price=None, take_profit_price=None, estimated_costs=None):
        self._require_init(); n = money(notional)
        if not symbol or side not in ("buy", "sell") or order_type != "market" or int(leverage) < 1: raise TraderError("UNSUPPORTED_CAPABILITY", "unsupported opening parameters")
        cost = money(estimated_costs if estimated_costs is not None else (n * Decimal("0.005")).quantize(Decimal("0.01"), rounding=ROUND_UP))
        status = self.trader_status(); available = money(status["available_to_open"])
        if n + cost > available: raise TraderError("INSUFFICIENT_BUDGET", "opening notional and estimated costs exceed trader budget")
        now = datetime.now(timezone.utc); params = {"symbol": symbol, "side": side, "notional": str(n), "leverage": int(leverage), "stop_loss_price": None if stop_loss_price is None else str(money(stop_loss_price)), "take_profit_price": None if take_profit_price is None else str(money(take_profit_price))}
        p = Preview(secrets.token_urlsafe(12), "open", now, now + timedelta(minutes=5), params, cost, available - n - cost, self.broker.state_fingerprint()); self._previews[p.preview_id] = p; return {"preview_id": p.preview_id, "expires_at": p.expires_at.isoformat(), "operation": "open", **params, "estimated_costs": str(cost), "remaining_budget_after_submit": str(p.remaining_budget), "broker_state_fingerprint": p.fingerprint}

    def preview_close(self, *, position_id, fraction=1):
        self._require_init()
        f = Decimal(str(fraction))
        if f <= 0 or f > 1: raise TraderError("INVALID_AMOUNT", "fraction must be in (0, 1]")
        owned = next((x for x in self._state["positions"] if x["id"] == position_id), None)
        if owned is None: raise TraderError("NOT_FOUND", "position not found")
        now = datetime.now(timezone.utc); params = {"position_id": position_id, "fraction": str(f)}
        p = Preview(secrets.token_urlsafe(12), "close", now, now + timedelta(minutes=5), params, Decimal("0"), money(self.trader_status()["available_to_open"]), self.broker.state_fingerprint())
        self._previews[p.preview_id] = p
        return {"preview_id": p.preview_id, "expires_at": p.expires_at.isoformat(), "operation": "close", **params, "broker_state_fingerprint": p.fingerprint}

    def submit(self, preview_id, idempotency_key):
        self._require_init()
        if not idempotency_key: raise TraderError("INVALID_IDEMPOTENCY_KEY", "idempotency key is required")
        with self._lock:
            if idempotency_key in self._state["keys"]:
                old = self._state["keys"][idempotency_key]
                if old.get("preview_id") != preview_id: raise TraderError("IDEMPOTENCY_CONFLICT", "key was already used for different parameters")
                return old["result"]
            p = self._previews.get(preview_id)
            if not p or p.expires_at < datetime.now(timezone.utc): raise TraderError("STALE_PREVIEW", "preview expired or unknown")
            if p.fingerprint != self.broker.state_fingerprint(): raise TraderError("STALE_PREVIEW", "broker state changed; preview again")
            if p.operation == "close":
                result = self.broker.close(**p.params)
                pos = next(x for x in self._state["positions"] if x["id"] == p.params["position_id"])
                released = money(Decimal(pos["notional"]) * Decimal(p.params["fraction"]))
                self._state["committed"] = str(max(Decimal("0"), money(self._state.get("committed", "0")) - released))
                if Decimal(p.params["fraction"]) >= 1: self._state["positions"] = [x for x in self._state["positions"] if x["id"] != p.params["position_id"]]
                else: pos["notional"] = str(money(Decimal(pos["notional"]) - released))
                out = {"status": "filled", "released": str(released), "realized_pnl": str(result.get("realized_pnl", Decimal("0")))}
                self._state["keys"][idempotency_key] = {"preview_id": preview_id, "result": out}; self._save(); return out
            if p.operation != "open": raise TraderError("UNSUPPORTED_CAPABILITY", "operation unsupported")
            current = money(self.trader_status()["available_to_open"])
            n, cost = money(p.params["notional"]), p.estimated_costs
            if n + cost > current: raise TraderError("INSUFFICIENT_BUDGET", "budget changed; preview again")
            pid = self.broker.open(**p.params); self._state["positions"].append({"id": pid, **p.params, "notional": str(n)})
            self._state["committed"] = str(money(self._state.get("committed", "0")) + n + cost)
            result = {"status": "filled", "intent_id": "intent_" + secrets.token_urlsafe(8), "broker_position_id": pid}
            self._state["keys"][idempotency_key] = {"preview_id": preview_id, "result": result}; self._save(); return result


__all__ = ["TraderService", "MemoryBroker", "Broker", "TraderError"]
