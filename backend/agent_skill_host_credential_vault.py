"""Encrypted credential adapter for external Formal Agent Skill hosts.

The SQLite host registry stores only an opaque reference and a short
fingerprint.  Secret material is delegated to the existing AES-256-GCM vault
whose data-encryption key is protected by Windows DPAPI CurrentUser.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.credential_store import fingerprint
from backend.persistent_credential_vault import (
    CredentialScope,
    CredentialVaultError,
    PersistentCredentialVault,
    Protector,
)
from backend.tenant_security import TenantScope


class HostCredentialVaultError(RuntimeError):
    pass


@dataclass(frozen=True)
class HostCredentialMetadata:
    secret_ref: str
    credential_fingerprint: str
    auth_type: str
    updated_at: str


class AgentSkillHostCredentialVault:
    """Host-scoped facade over :class:`PersistentCredentialVault`."""

    SUPPORTED_AUTH = frozenset({"none", "bearer", "api_key_header", "oauth2_client_credentials"})

    def __init__(self, scope: TenantScope, directory: Path | None = None,
                 protector: Protector | None = None) -> None:
        self.scope = scope
        self._vault = PersistentCredentialVault(directory=directory, protector=protector)

    def _scope(self, host_id: str) -> CredentialScope:
        if not host_id or len(host_id) > 128 or any(c in host_id for c in "\r\n\x00"):
            raise HostCredentialVaultError("host_id_invalid")
        encoded = base64.urlsafe_b64encode(host_id.encode()).decode().rstrip("=")
        return CredentialScope("formal-agent-skill-host", self.scope.tenant_id,
                               self.scope.workspace_id, f"host-{encoded}")

    def reference(self, host_id: str) -> str:
        return "ASHC-" + self._scope(host_id).digest()

    @staticmethod
    def _normalize(auth_type: str, credential: str | dict[str, Any] | None) -> dict[str, str]:
        if auth_type not in AgentSkillHostCredentialVault.SUPPORTED_AUTH:
            raise HostCredentialVaultError("host_auth_type_invalid")
        if auth_type == "none":
            return {}
        if isinstance(credential, str):
            payload = {"secret": credential}
        elif isinstance(credential, dict):
            payload = {str(k): str(v) for k, v in credential.items()}
        else:
            raise HostCredentialVaultError("host_credential_invalid")
        required = {
            "bearer": ("secret",),
            "api_key_header": ("secret",),
            "oauth2_client_credentials": ("client_id", "client_secret", "token_url"),
        }[auth_type]
        if any(not payload.get(key, "").strip() for key in required):
            raise HostCredentialVaultError("host_credential_invalid")
        if any(len(value) > 4096 or "\x00" in value for value in payload.values()):
            raise HostCredentialVaultError("host_credential_invalid")
        return payload

    def save(self, host_id: str, auth_type: str,
             credential: str | dict[str, Any] | None) -> HostCredentialMetadata:
        payload = self._normalize(auth_type, credential)
        if auth_type == "none":
            self.delete(host_id)
            return HostCredentialMetadata("", "", auth_type, "")
        canonical = json.dumps({"auth_type": auth_type, "values": payload},
                               sort_keys=True, separators=(",", ":"))
        # PersistentCredentialVault accepts printable ASCII.  Base64 also keeps
        # arbitrary client identifiers out of the encrypted envelope metadata.
        encoded = base64.urlsafe_b64encode(canonical.encode()).decode()
        try:
            metadata = self._vault.save(self._scope(host_id), encoded)
        except CredentialVaultError as exc:
            raise HostCredentialVaultError(str(exc)) from exc
        secret_material = "\x1f".join(payload[key] for key in sorted(payload))
        return HostCredentialMetadata(
            self.reference(host_id), fingerprint(secret_material), auth_type,
            str(metadata["updated_at"]),
        )

    def load(self, host_id: str, secret_ref: str) -> tuple[str, dict[str, str]]:
        if not secret_ref or secret_ref != self.reference(host_id):
            raise HostCredentialVaultError("host_credential_reference_invalid")
        try:
            loaded = self._vault.load(self._scope(host_id), required=True)
        except CredentialVaultError as exc:
            raise HostCredentialVaultError(str(exc)) from exc
        assert loaded is not None
        try:
            payload = json.loads(base64.urlsafe_b64decode(loaded[0]).decode())
            auth_type = str(payload["auth_type"])
            values = {str(k): str(v) for k, v in payload["values"].items()}
            self._normalize(auth_type, values)
            return auth_type, values
        except Exception as exc:
            raise HostCredentialVaultError("host_credential_corrupted") from exc

    def delete(self, host_id: str) -> bool:
        try:
            return self._vault.delete(self._scope(host_id))
        except CredentialVaultError as exc:
            raise HostCredentialVaultError(str(exc)) from exc

