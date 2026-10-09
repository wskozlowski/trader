"""Browser-safe formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal


def format_decimal(value: Decimal | None, *, places: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{places}f}"


def format_money(value: Decimal | None, currency: str) -> str:
    return f"{'Waiting' if value is None else format_decimal(value, places=2)} {currency}"


def format_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def unavailable(value: Decimal | None) -> tuple[str, bool]:
    return (format_decimal(value), value is None)
