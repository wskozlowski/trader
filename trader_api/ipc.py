from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
from pathlib import Path
from typing import Any

from .errors import TraderError

MAX_MESSAGE = 1_048_576


def _sign(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _receive(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise TraderError("IPC_INVALID", "IPC connection closed early")
        result.extend(chunk)
    return bytes(result)


class IpcClient:
    """Authenticated value-only local IPC; database and broker handles never cross it."""

    def __init__(self, socket_path: Path, service_credential: str) -> None:
        self.socket_path = socket_path
        self.service_credential = service_credential

    def call(self, method: str, params: dict[str, Any]) -> Any:
        body = json.dumps({"method": method, "params": params}, sort_keys=True).encode()
        frame = json.dumps({"body": body.decode(), "signature": _sign(self.service_credential, body)}).encode()
        if len(frame) > MAX_MESSAGE:
            raise TraderError("IPC_INVALID", "IPC message is too large")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(self.socket_path))
            connection.sendall(len(frame).to_bytes(4, "big") + frame)
            length = int.from_bytes(_receive(connection, 4), "big")
            response = json.loads(_receive(connection, length))
        if response["error"] is not None:
            error = response["error"]
            raise TraderError(error["code"], error["message"], retryable=error["retryable"], details=error["details"])
        return response["data"]


class IpcWorker:
    ALLOWED_METHODS = frozenset(
        {
            "capabilities",
            "trader_status",
            "portfolio",
            "initialize",
            "positions",
            "orders",
            "preview_open",
            "preview_modify",
            "preview_close",
            "preview_cancel",
            "submit",
            "intent_status",
            "reconcile",
            "audit_events",
            "portfolio_history",
            "trends",
            "verify_audit",
        }
    )

    def __init__(self, socket_path: Path, service: Any, service_credential: str) -> None:
        self.socket_path = socket_path
        self.service = service
        self.service_credential = service_credential

    def serve_once(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.socket_path.exists():
            raise TraderError("IPC_INVALID", "worker socket already exists")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(self.socket_path))
                os.chmod(self.socket_path, 0o600)
                server.listen(1)
                connection, _address = server.accept()
                with connection:
                    length = int.from_bytes(_receive(connection, 4), "big")
                    if length > MAX_MESSAGE:
                        raise TraderError("IPC_INVALID", "IPC message is too large")
                    frame = json.loads(_receive(connection, length))
                    body = frame["body"].encode()
                    if not hmac.compare_digest(frame["signature"], _sign(self.service_credential, body)):
                        error = TraderError("AUTHENTICATION_REQUIRED", "IPC authentication failed")
                        response = {"data": None, "error": error.as_dict()}
                    else:
                        response = self._invoke(json.loads(body))
                    encoded = json.dumps(response, default=str).encode()
                    connection.sendall(len(encoded).to_bytes(4, "big") + encoded)
        finally:
            self.socket_path.unlink(missing_ok=True)

    def _invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        method = request.get("method")
        if method not in self.ALLOWED_METHODS:
            return {"data": None, "error": TraderError("IPC_INVALID", "method is not exposed").as_dict()}
        try:
            return {"data": getattr(self.service, method)(**request.get("params", {})), "error": None}
        except TraderError as exc:
            return {"data": None, "error": exc.as_dict()}
