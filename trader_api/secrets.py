from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import TraderError


class CredentialVault:
    """Small envelope store for one-time child tokens; plaintext is never audited."""

    def __init__(self, directory: Path, master_key: str) -> None:
        if len(master_key) < 24:
            raise TraderError("CONFIG_INVALID", "credential-vault master key is too short")
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self._key = hashlib.sha256(master_key.encode()).digest()

    def put(self, reference: str, secret: str) -> str:
        if not reference or not secret:
            raise TraderError("SECRET_INVALID", "secret reference and value are required")
        filename = hashlib.sha256(reference.encode()).hexdigest() + ".secret"
        path = self.directory / filename
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, secret.encode(), reference.encode())
        encoded = base64.urlsafe_b64encode(nonce + ciphertext)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise TraderError("SECRET_CONFLICT", "credential reference already exists") from exc
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return f"vault:{filename[:-7]}"

    def get(self, reference: str, vault_reference: str) -> str:
        if not vault_reference.startswith("vault:"):
            raise TraderError("SECRET_INVALID", "invalid credential reference")
        path = self.directory / (vault_reference.removeprefix("vault:") + ".secret")
        try:
            raw = base64.urlsafe_b64decode(path.read_bytes())
            return AESGCM(self._key).decrypt(raw[:12], raw[12:], reference.encode()).decode()
        except (OSError, ValueError) as exc:
            raise TraderError("SECRET_UNAVAILABLE", "credential cannot be recovered") from exc
