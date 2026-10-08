from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..config import Profile, Route
from ..errors import TraderError

_SECRET_FIELDS = re.compile(r"(?i)(api[-_]?key|user[-_]?key|authorization|token|secret)")


class RollingRateLimiter:
    """Conservative local guard; the broker remains authoritative across processes."""

    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self.clock = clock
        self._all: deque[float] = deque()
        self._mutations: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, *, mutation: bool) -> None:
        now = float(self.clock())
        cutoff = now - 60
        with self._lock:
            while self._all and self._all[0] <= cutoff:
                self._all.popleft()
            while self._mutations and self._mutations[0] <= cutoff:
                self._mutations.popleft()
            if len(self._all) >= 20 or (mutation and len(self._mutations) >= 16):
                raise TraderError(
                    "BROKER_RATE_LIMITED",
                    "local rolling broker quota is exhausted",
                    retryable=True,
                )
            self._all.append(now)
            if mutation:
                self._mutations.append(now)


def redact(value: object, secrets: tuple[str, ...] = ()) -> object:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _SECRET_FIELDS.search(str(key)) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "<redacted>")
        return result
    return value


class HttpTransport:
    def __init__(
        self,
        profile: Profile,
        client: httpx.Client | None = None,
        limiter: RollingRateLimiter | None = None,
    ) -> None:
        self.profile = profile
        self.client = client or httpx.Client(timeout=profile.timeout_seconds, follow_redirects=False)
        self.limiter = limiter or RollingRateLimiter()

    def request(
        self,
        route: Route,
        *,
        # Caller correlation value sent as x-request-id; a local UUID is generated if absent.
        request_id: str | None = None,
        # External numeric eToro orderId/positionId path values, not memo IDs.
        path_values: dict[str, int] | None = None,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = route.url
        for key, value in (path_values or {}).items():
            if not isinstance(value, int) or value < 0:
                raise TraderError("INVALID_BROKER_ID", "broker identifiers must be non-negative integers")
            url = url.replace("{" + key + "}", str(value))
        if "{" in url or "}" in url:
            raise TraderError("CONFIG_INVALID", "operation URL has an unresolved path placeholder")
        correlation = request_id or str(uuid.uuid4())
        headers = {
            "x-api-key": self.profile.api_key,
            "x-user-key": self.profile.user_key,
            "x-request-id": correlation,
            "accept": "application/json",
        }
        mutation = route.name in {
            "ETORO_OPEN_ORDER_URL",
            "ETORO_CANCEL_ORDER_URL",
            "ETORO_CLOSE_POSITION_URL",
            "ETORO_CANCEL_CLOSE_ORDER_URL",
            "ETORO_MODIFY_POSITION_URL",
        }
        self.limiter.acquire(mutation=mutation)
        try:
            response = self.client.request(route.method, url, headers=headers, params=params, json=json_body)
        except httpx.TimeoutException as exc:
            raise TraderError("BROKER_OUTCOME_UNKNOWN", "broker request timed out", retryable=False) from exc
        except httpx.HTTPError as exc:
            raise TraderError("BROKER_UNAVAILABLE", "broker transport failed", retryable=True) from exc
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after", "")
            seconds: float | None = None
            try:
                seconds = float(int(retry_after))
            except ValueError:
                with suppress(ValueError, TypeError, OverflowError):
                    seconds = (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()
            raise TraderError(
                "BROKER_RATE_LIMITED", "broker rate limit reached", retryable=True,
                details={} if seconds is None else {"retry_after_seconds": max(0, seconds)},
            )
        if response.status_code in {401, 403}:
            raise TraderError("PERMISSION_REVOKED", "broker rejected the verified credential")
        if response.is_redirect:
            raise TraderError("BROKER_PROTOCOL_ERROR", "broker redirects are not accepted")
        if response.status_code >= 400:
            raise TraderError(
                "BROKER_REJECTED",
                "broker rejected the request",
                details={"status_code": response.status_code},
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TraderError("BROKER_PROTOCOL_ERROR", "broker returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise TraderError("BROKER_PROTOCOL_ERROR", "broker response must be an object")
        return payload
