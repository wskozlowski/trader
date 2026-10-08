from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from trader_api.errors import TraderError
from trader_api.ipc import IpcClient, IpcWorker
from trader_api.secrets import CredentialVault


class EchoService:
    def capabilities(self) -> dict[str, bool]:
        return {"ok": True}


def test_authenticated_ipc_value_boundary(tmp_path: Path) -> None:
    socket_path = tmp_path / "worker.sock"
    worker = IpcWorker(socket_path, EchoService(), "credential")
    thread = threading.Thread(target=worker.serve_once)
    thread.start()
    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.005)
    assert IpcClient(socket_path, "credential").call("capabilities", {}) == {"ok": True}
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_ipc_method_allowlist() -> None:
    worker = IpcWorker(Path("/unused"), EchoService(), "credential")
    response = worker._invoke({"method": "__dict__", "params": {}})
    assert response["error"]["code"] == "IPC_INVALID"


def test_encrypted_credential_vault(tmp_path: Path) -> None:
    vault = CredentialVault(tmp_path / "vault", "a-sufficiently-long-owner-master-key")
    reference = vault.put("child-1", "issued-secret")
    assert vault.get("child-1", reference) == "issued-secret"
    stored = next((tmp_path / "vault").iterdir()).read_bytes()
    assert b"issued-secret" not in stored
    with pytest.raises(TraderError):
        vault.put("child-1", "duplicate")
