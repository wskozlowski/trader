from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from .domain import Environment
from .errors import TraderError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRUSTED_HOST = "public-api.etoro.com"

_ROUTES: dict[str, tuple[str, dict[Environment, str]]] = {
    "ETORO_PNL_URL": (
        "GET",
        {Environment.DEMO: r"/api/v1/trading/info/demo/pnl", Environment.REAL: r"/api/v1/trading/info/real/pnl"},
    ),
    "ETORO_OPEN_ORDER_URL": (
        "POST",
        {
            Environment.DEMO: r"/api/v2/trading/execution/demo/orders",
            Environment.REAL: r"/api/v2/trading/execution/orders",
        },
    ),
    "ETORO_ORDER_LOOKUP_URL": (
        "GET",
        {
            Environment.DEMO: r"/api/v2/trading/info/demo/orders:lookup",
            Environment.REAL: r"/api/v2/trading/info/orders:lookup",
        },
    ),
    "ETORO_CANCEL_ORDER_URL": (
        "DELETE",
        {
            Environment.DEMO: r"/api/v2/trading/execution/demo/orders/\{orderId\}",
            Environment.REAL: r"/api/v2/trading/execution/orders/\{orderId\}",
        },
    ),
    "ETORO_CLOSE_POSITION_URL": (
        "POST",
        {
            Environment.DEMO: r"/api/v1/trading/execution/demo/market-close-orders/positions/\{positionId\}",
            Environment.REAL: r"/api/v1/trading/execution/market-close-orders/positions/\{positionId\}",
        },
    ),
    "ETORO_CLOSE_ORDER_LOOKUP_URL": (
        "GET",
        {
            Environment.DEMO: r"/api/v1/trading/info/demo/close-orders/\{orderId\}",
            Environment.REAL: r"/api/v1/trading/info/close-orders/\{orderId\}",
        },
    ),
    "ETORO_CANCEL_CLOSE_ORDER_URL": (
        "DELETE",
        {
            Environment.DEMO: r"/api/v1/trading/execution/demo/market-close-orders/\{orderId\}",
            Environment.REAL: r"/api/v1/trading/execution/market-close-orders/\{orderId\}",
        },
    ),
    "ETORO_MODIFY_POSITION_URL": (
        "PATCH",
        {
            Environment.DEMO: r"/api/v2/trading/demo/positions/\{positionId\}",
            Environment.REAL: r"/api/v2/trading/positions/\{positionId\}",
        },
    ),
    "ETORO_TRADING_HISTORY_URL": (
        "GET",
        {
            Environment.DEMO: r"/api/v1/trading/info/trade/demo/history",
            Environment.REAL: r"/api/v1/trading/info/trade/history",
        },
    ),
    "ETORO_TRADING_COSTS_URL": (
        "POST",
        {Environment.DEMO: r"/api/v2/trading/info/demo/costs", Environment.REAL: r"/api/v2/trading/info/costs"},
    ),
    "ETORO_TRADING_ELIGIBILITY_URL": (
        "POST",
        {
            Environment.DEMO: r"/api/v2/trading/info/demo/eligibility",
            Environment.REAL: r"/api/v2/trading/info/eligibility",
        },
    ),
}

_NEUTRAL_ROUTES: dict[str, tuple[str, str]] = {
    "ETORO_MARKET_RATES_URL": ("GET", r"/api/v1/market-data/instruments/rates"),
}


@dataclass(frozen=True, slots=True)
class Route:
    name: str
    method: str
    url: str
    environment: Environment | None


@dataclass(frozen=True, slots=True)
class Profile:
    path: Path
    api_key: str
    user_key: str
    service_credential: str | None
    owner_service_credential: str | None
    vault_master_key: str | None
    routes: MappingProxyType[str, Route]
    timeout_seconds: float

    def route(self, name: str) -> Route | None:
        return self.routes.get(name)


def _profile_path(value: str | Path) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise TraderError("CONFIG_INVALID", "config profile must be a filename in the project root")
    resolved = (PROJECT_ROOT / candidate).resolve()
    if resolved.parent != PROJECT_ROOT.resolve():
        raise TraderError("CONFIG_INVALID", "config profile escapes the fixed project root")
    return resolved


def _parse_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise TraderError("CONFIG_INVALID", "selected config profile is missing")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise TraderError("CONFIG_PERMISSIONS", "config profile must have mode 0600")
    values: dict[str, str] = {}
    for number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise TraderError("CONFIG_INVALID", f"invalid config line {number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise TraderError("CONFIG_INVALID", f"invalid or duplicate config key at line {number}")
        values[key] = value.strip()
    if "TRADER_ENV" in values:
        raise TraderError(
            "CONFIG_INVALID",
            "TRADER_ENV is obsolete; remove it because environment is derived from verified user-token scopes",
        )
    return values


def _validate_url(name: str, url: str) -> Route:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != TRUSTED_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise TraderError("CONFIG_INVALID", f"{name} is not an allowed broker operation URL")
    decoded_path = unquote(parsed.path)
    if ".." in decoded_path.split("/") or unquote(decoded_path) != decoded_path:
        raise TraderError("CONFIG_INVALID", f"{name} contains path traversal or double encoding")
    if name in _NEUTRAL_ROUTES:
        method, pattern = _NEUTRAL_ROUTES[name]
        if not re.fullmatch(pattern, decoded_path):
            raise TraderError("CONFIG_INVALID", f"{name} path is not allowlisted")
        return Route(name, method, url, None)
    method, patterns = _ROUTES[name]
    matches = [environment for environment, pattern in patterns.items() if re.fullmatch(pattern, decoded_path)]
    if len(matches) != 1:
        raise TraderError("CONFIG_INVALID", f"{name} path is not allowlisted")
    return Route(name, method, url, matches[0])


def load_profile(config_profile: str | Path = ".env") -> Profile:
    """Load exactly one file. Ambient variables are intentionally ignored."""
    path = _profile_path(config_profile)
    values = _parse_dotenv(path)
    api_key = values.get("ETORO_API_KEY", "")
    user_key = values.get("ETORO_USER_KEY", "")
    if not api_key or not user_key:
        raise TraderError("CONFIG_INVALID", "selected profile lacks broker credentials")
    routes = {
        name: _validate_url(name, value) for name, value in values.items() if name in _ROUTES or name in _NEUTRAL_ROUTES
    }
    unknown_urls = [key for key in values if key.endswith("_URL") and key not in _ROUTES and key not in _NEUTRAL_ROUTES]
    if unknown_urls:
        raise TraderError("CONFIG_INVALID", "selected profile contains an unknown operation URL")
    try:
        timeout = float(values.get("TRADER_HTTP_TIMEOUT_SECONDS", "10"))
    except ValueError as exc:
        raise TraderError("CONFIG_INVALID", "invalid HTTP timeout") from exc
    if not 0 < timeout <= 60:
        raise TraderError("CONFIG_INVALID", "HTTP timeout must be in (0, 60]")
    return Profile(
        path=path,
        api_key=api_key,
        user_key=user_key,
        service_credential=values.get("TRADER_SERVICE_CREDENTIAL") or None,
        owner_service_credential=values.get("TRADER_OWNER_SERVICE_CREDENTIAL") or None,
        vault_master_key=values.get("TRADER_VAULT_MASTER_KEY") or None,
        routes=MappingProxyType(routes),
        timeout_seconds=timeout,
    )


def route_environment(profile: Profile) -> Environment | None:
    environments = {route.environment for route in profile.routes.values() if route.environment is not None}
    if len(environments) > 1:
        raise TraderError("ENDPOINT_ENVIRONMENT_MISMATCH", "configured operation routes mix environments")
    return next(iter(environments), None)


def fixed_storage_root(environment: Environment) -> Path:
    return Path("/dbzero-data/trader-dev" if environment is Environment.DEMO else "/dbzero-data/trader")


def credential_file_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
