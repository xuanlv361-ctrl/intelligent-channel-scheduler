"""Windows-user encrypted persistence for operator-supplied API credentials."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.credential_store import fingerprint
from backend.encrypted_session_vault import WindowsCurrentUserDPAPI
from backend.tenant_security import TenantScope


VAULT_VERSION = "uat-credential-v1"


class CredentialVaultError(RuntimeError):
    pass


class Protector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...
    def unprotect(self, ciphertext: bytes) -> bytes: ...


def default_credential_directory() -> Path:
    local = os.getenv("LOCALAPPDATA")
    if not local:
        raise CredentialVaultError("windows_credential_vault_unavailable")
    return Path(local) / "IntelligentChannelScheduler" / "credentials"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


@dataclass(frozen=True)
class CredentialScope:
    principal_id: str
    tenant_id: str
    workspace_id: str
    environment_id: str

    def __post_init__(self) -> None:
        TenantScope(self.tenant_id, self.workspace_id)
        for value in (self.principal_id, self.environment_id):
            if not value or len(value) > 256 or "\x00" in value:
                raise CredentialVaultError("credential_scope_invalid")

    def canonical(self) -> dict[str, str]:
        return {
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
            "workspace_id": self.workspace_id,
            "environment_id": self.environment_id,
        }

    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.canonical())).hexdigest()


class PersistentCredentialVault:
    """Atomically stores secrets encrypted for the current Windows user.

    No plaintext credential, principal identifier, tenant identifier or workspace
    identifier is present in filenames or unencrypted file content.
    """

    def __init__(self, directory: Path | None = None,
                 protector: Protector | None = None) -> None:
        self.directory = Path(directory) if directory else default_credential_directory()
        self.protector = protector or WindowsCurrentUserDPAPI()
        self._lock = threading.RLock()

    def _path(self, scope: CredentialScope) -> Path:
        return self.directory / f"{scope.digest()}.uatkey"

    def _atomic_write(self, destination: Path, content: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, destination)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _metadata(payload: dict[str, str]) -> dict[str, str | None]:
        return {
            "created_at": payload["created_at"],
            "updated_at": payload["updated_at"],
            "expires_at": None,
            "minutes_remaining": None,
            "key_fingerprint": payload["key_fingerprint"],
            "credential_source": "windows_encrypted_vault",
            "environment_id": payload["environment_id"],
            "encryption": "AES-256-GCM",
            "key_protection": "DPAPI-CurrentUser",
        }

    def save(self, scope: CredentialScope, value: str,
             when: datetime | None = None) -> dict[str, str | None]:
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise CredentialVaultError("credential_invalid")
        key = value.strip()
        if not key or len(key) > 4096 or any(ord(char) < 32 or ord(char) > 126 for char in key):
            raise CredentialVaultError("credential_invalid")
        now = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        previous = self.load(scope, required=False)
        payload = {
            "schema_version": VAULT_VERSION,
            **scope.canonical(),
            "api_key": key,
            "key_fingerprint": fingerprint(key),
            "created_at": previous[1]["created_at"] if previous else now,
            "updated_at": now,
        }
        plaintext = bytearray(_canonical(payload))
        aad = _canonical({"schema_version": VAULT_VERSION, "scope_digest": scope.digest()})
        dek = bytearray(os.urandom(32))
        nonce = os.urandom(12)
        try:
            ciphertext = AESGCM(bytes(dek)).encrypt(nonce, bytes(plaintext), aad)
            wrapped_key = self.protector.protect(bytes(dek))
            envelope = _canonical({
                "schema_version": VAULT_VERSION,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "wrapped_key": base64.b64encode(wrapped_key).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            })
            with self._lock:
                self._atomic_write(self._path(scope), envelope)
        except CredentialVaultError:
            raise
        except Exception as exc:
            raise CredentialVaultError("credential_persistence_failed") from exc
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
            dek[:] = b"\x00" * len(dek)
        return self._metadata(payload)

    def load(self, scope: CredentialScope, *, required: bool = False
             ) -> tuple[str, dict[str, str | None]] | None:
        path = self._path(scope)
        try:
            with self._lock:
                envelope_bytes = path.read_bytes()
        except FileNotFoundError:
            if required:
                raise CredentialVaultError("credential_not_configured")
            return None
        try:
            envelope = json.loads(envelope_bytes)
            if envelope.get("schema_version") != VAULT_VERSION:
                raise CredentialVaultError("credential_vault_version_invalid")
            wrapped = base64.b64decode(envelope["wrapped_key"], validate=True)
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            aad = _canonical({"schema_version": VAULT_VERSION,
                              "scope_digest": scope.digest()})
            dek = bytearray(self.protector.unprotect(wrapped))
            plaintext = bytearray(AESGCM(bytes(dek)).decrypt(nonce, ciphertext, aad))
            payload = json.loads(bytes(plaintext))
            if payload.get("schema_version") != VAULT_VERSION or any(
                payload.get(key) != value for key, value in scope.canonical().items()
            ):
                raise CredentialVaultError("credential_scope_mismatch")
            value = str(payload.get("api_key") or "")
            if not value or fingerprint(value) != payload.get("key_fingerprint"):
                raise CredentialVaultError("credential_vault_corrupted")
            return value, self._metadata(payload)
        except CredentialVaultError:
            raise
        except InvalidTag as exc:
            raise CredentialVaultError("credential_vault_corrupted") from exc
        except Exception as exc:
            raise CredentialVaultError("credential_decryption_failed") from exc
        finally:
            if "dek" in locals():
                dek[:] = b"\x00" * len(dek)
            if "plaintext" in locals():
                plaintext[:] = b"\x00" * len(plaintext)

    def metadata(self, scope: CredentialScope) -> dict[str, str | None] | None:
        loaded = self.load(scope, required=False)
        return loaded[1] if loaded else None

    def delete(self, scope: CredentialScope) -> bool:
        try:
            with self._lock:
                self._path(scope).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CredentialVaultError("credential_delete_failed") from exc

    def delete_principal(self, principal_id: str, tenant_id: str,
                         workspace_id: str, environments: tuple[str, ...]) -> int:
        return sum(self.delete(CredentialScope(
            principal_id, tenant_id, workspace_id, environment_id
        )) for environment_id in environments)
