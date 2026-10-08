from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

import dbzero as db0  # type: ignore[import-untyped]


def storage_datetime(value: datetime) -> datetime:
    """Use UTC and the millisecond precision of dbzero's aware datetime storage."""
    if value.tzinfo is None:
        raise ValueError("persistent timestamps must be timezone-aware")
    value = value.astimezone(UTC)
    return value.replace(microsecond=value.microsecond // 1000 * 1000)


def native_copy(value: Any) -> Any:
    """Detach persistent containers without serializing their scalar values."""
    if hasattr(value, "items"):
        return {key: native_copy(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)) or (
        not isinstance(value, (str, bytes)) and hasattr(value, "__getitem__") and hasattr(value, "__len__")
    ):
        return [native_copy(item) for item in value]
    if isinstance(value, datetime):
        return storage_datetime(value)
    return value


def api_value(value: Any) -> Any:
    """Convert native model values at JSON, API, and audit-hashing boundaries."""
    if hasattr(value, "items"):
        return {key: api_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)) or (
        not isinstance(value, (str, bytes)) and hasattr(value, "__getitem__") and hasattr(value, "__len__")
    ):
        return [api_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal) or db0.is_enum_value(value):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Unsupported serialized value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(api_value(value), sort_keys=True, separators=(",", ":"))
