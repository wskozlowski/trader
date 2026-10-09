"""Strict server-side trader catalog parsing."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..errors import TraderError

_KEY = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    key: str
    label: str
    trader_name: str
    profile_filename: str
    trading_mode: str = "bound"


@dataclass(frozen=True, slots=True)
class Catalog:
    version: int
    environment: str
    entries: tuple[CatalogEntry, ...]

    def by_key(self, key: str) -> CatalogEntry:
        for entry in self.entries:
            if entry.key == key:
                return entry
        raise TraderError("UNKNOWN_TRADER", "select a configured trader")


def default_catalog(environment: str) -> Catalog:
    """Return the safe single-trader catalog used when no file is supplied."""
    if environment not in {"demo", "real"}:
        raise _fail("environment must be demo or real")
    profile = ".env_demo" if environment == "demo" else ".env_real"
    return Catalog(
        1,
        environment,
        (
            CatalogEntry(
                "alpha",
                f"{environment.title()} trader",
                "alpha",
                profile,
                "standalone" if environment == "demo" else "bound",
            ),
        ),
    )


def _fail(message: str) -> TraderError:
    return TraderError("INVALID_CATALOG", message)


def load_catalog(path: str | Path, *, expected_environment: str) -> Catalog:
    """Load and validate the complete catalog before the server listens."""
    if expected_environment not in {"demo", "real"}:
        raise _fail("environment must be demo or real")
    try:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _fail("catalog could not be read") from exc
    if set(raw) != {"version", "environment", "traders"}:
        raise _fail("catalog contains unknown top-level fields")
    if raw["version"] != 1 or raw["environment"] != expected_environment:
        raise _fail("catalog version or environment does not match")
    traders = raw["traders"]
    if not isinstance(traders, dict) or not traders:
        raise _fail("catalog must contain traders")
    entries: list[CatalogEntry] = []
    seen_names: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for key, value in traders.items():
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise _fail("invalid trader catalog key")
        if not isinstance(value, dict) or set(value) not in (
            {"label", "trader", "profile"},
            {"label", "trader", "profile", "mode"},
        ):
            raise _fail(f"invalid fields for trader {key}")
        mode = value.get("mode", "bound")
        if mode not in {"bound", "standalone"}:
            raise _fail(f"invalid trading mode for trader {key}")
        label, trader, profile = value["label"], value["trader"], value["profile"]
        if not all(isinstance(x, str) and x.strip() for x in (label, trader, profile)):
            raise _fail(f"invalid values for trader {key}")
        if len(label) > 200 or len(trader) > 128 or len(profile) > 255:
            raise _fail(f"value too long for trader {key}")
        profile_path = Path(profile)
        if profile_path.is_absolute() or len(profile_path.parts) != 1 or profile in {".", ".."}:
            raise _fail("profile must be a filename")
        normalized = (trader, profile_path.name)
        if trader in seen_names or normalized in seen_pairs:
            raise _fail("duplicate trader runtime")
        seen_names.add(trader)
        seen_pairs.add(normalized)
        entries.append(CatalogEntry(key, label, trader, profile_path.name, mode))
    return Catalog(1, expected_environment, tuple(entries))
