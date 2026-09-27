from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import tempfile
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from backend.tenant_security import TenantScope

APPROVED_ORIGINS = frozenset({
    "https://uat.weimeta.cn",
    "https://admin-uat.weimeta.cn",
})
APPROVED_HOSTS = frozenset(urlsplit(origin).hostname for origin in APPROVED_ORIGINS)
VAULT_VERSION = "2.0"


class VaultError(ValueError):
    pass


class Protector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...
    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsCurrentUserDPAPI:
    """Protects only the random AES DEK with Windows DPAPI CurrentUser."""

    def __init__(self):
        if os.name != "nt":
            raise VaultError("persistent_mode_windows_only")
        self.crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        self.crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        self.crypt32.CryptProtectData.restype = wintypes.BOOL
        self.crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        self.crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        self.kernel32.LocalFree.restype = wintypes.HLOCAL

    @staticmethod
    def _input(data: bytes) -> tuple[_DataBlob, Any]:
        buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        return _DataBlob(len(data), buffer), buffer

    def protect(self, plaintext: bytes) -> bytes:
        source, keepalive = self._input(plaintext)
        output = _DataBlob()
        if not self.crypt32.CryptProtectData(
                ctypes.byref(source), "RoutingQualityConsole AES session key",
                None, None, None, 0x1, ctypes.byref(output)):
            raise VaultError("persistent_session_save_failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            self.kernel32.LocalFree(output.pbData)
            del keepalive

    def unprotect(self, ciphertext: bytes) -> bytes:
        source, keepalive = self._input(ciphertext)
        output = _DataBlob()
        description = wintypes.LPWSTR()
        if not self.crypt32.CryptUnprotectData(
                ctypes.byref(source), ctypes.byref(description), None,
                None, None, 0x1, ctypes.byref(output)):
            raise VaultError("persistent_session_decryption_failed")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if description:
                self.kernel32.LocalFree(description)
            self.kernel32.LocalFree(output.pbData)
            del keepalive


def default_vault_directory() -> Path:
    local = os.getenv("LOCALAPPDATA")
    if not local:
        raise VaultError("persistent_mode_windows_only")
    return Path(local) / "RoutingQualityConsole" / "session_vault"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")


def _exact_origin(value: str, approved_origins: frozenset[str]) -> str:
    parsed = urlsplit(str(value))
    if parsed.scheme != "https" or parsed.username or parsed.password or \
            parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise VaultError("persistent_session_origin_not_allowed")
    origin = f"https://{parsed.hostname.casefold()}" if parsed.hostname else ""
    try:
        port = parsed.port
    except ValueError as exc:
        raise VaultError("persistent_session_origin_not_allowed") from exc
    if port not in {None, 443} or origin not in approved_origins:
        raise VaultError("persistent_session_origin_not_allowed")
    return origin


class EncryptedSessionVault:
    def __init__(
        self, directory: Path | None = None, protector: Protector | None = None,
        approved_origins: set[str] | frozenset[str] | None = None,
        maximum_vault_bytes: int = 4 * 1024 * 1024,
        maximum_item_count: int = 512,
        maximum_value_bytes: int = 256 * 1024,
        allowed_storage_keys: set[str] | None = None,
    ):
        self.directory = Path(directory) if directory else default_vault_directory()
        self.protector = protector or WindowsCurrentUserDPAPI()
        configured = frozenset(approved_origins or APPROVED_ORIGINS)
        self.approved_origins = frozenset(
            _exact_origin(origin, frozenset(APPROVED_ORIGINS)) for origin in configured)
        self.maximum_vault_bytes = int(maximum_vault_bytes)
        self.maximum_item_count = int(maximum_item_count)
        self.maximum_value_bytes = int(maximum_value_bytes)
        self.allowed_storage_keys = (
            frozenset(allowed_storage_keys) if allowed_storage_keys is not None else None)
        if min(self.maximum_vault_bytes, self.maximum_item_count,
               self.maximum_value_bytes) <= 0:
            raise VaultError("persistent_session_vault_limits_invalid")

    def _paths(self, reference_id: str) -> tuple[Path, Path]:
        if not reference_id.startswith("VAULT-") or not reference_id[6:].isalnum():
            raise VaultError("persistent_session_not_found")
        # Keep the legacy extension so upgrades do not leave untracked secret files.
        return (self.directory / f"{reference_id}.dpapi",
                self.directory / f"{reference_id}.metadata.json")

    def _atomic_write(self, destination: Path, content: bytes):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=self.directory)
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

    def _approved_cookies(self, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        approved = []
        approved_hosts = {urlsplit(origin).hostname for origin in self.approved_origins}
        for cookie in cookies:
            domain = str(cookie.get("domain") or "").lstrip(".").casefold()
            if domain not in approved_hosts:
                continue
            safe = {
                key: cookie[key] for key in (
                    "name", "value", "domain", "path", "expires",
                    "httpOnly", "secure", "sameSite",
                ) if key in cookie
            }
            if not safe.get("name") or not safe.get("value"):
                continue
            self._validate_secret_item(str(safe["name"]), str(safe["value"]), False)
            approved.append(safe)
        return approved

    def _validate_secret_item(self, name: str, value: str, enforce_allowlist: bool = True):
        if not name or "\x00" in name:
            raise VaultError("persistent_session_storage_key_invalid")
        if enforce_allowlist and self.allowed_storage_keys is not None and \
                name not in self.allowed_storage_keys:
            raise VaultError("persistent_session_storage_key_not_allowed")
        if len(value.encode("utf-8")) > self.maximum_value_bytes:
            raise VaultError("persistent_session_storage_value_too_large")

    def _approved_web_storage(
        self, web_storage: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        result = []
        for origin_state in web_storage or []:
            origin = _exact_origin(origin_state.get("origin", ""), self.approved_origins)
            clean: dict[str, Any] = {"origin": origin}
            for storage_type in ("local_storage", "session_storage"):
                items = []
                for item in origin_state.get(storage_type, []) or []:
                    name, value = str(item.get("name") or ""), str(item.get("value") or "")
                    self._validate_secret_item(name, value)
                    items.append({"name": name, "value": value})
                clean[storage_type] = items
            result.append(clean)
        return sorted(result, key=lambda item: item["origin"])

    def _approved_indexed_db(
        self, indexed_db: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        result = []
        for origin_state in indexed_db or []:
            origin = _exact_origin(origin_state.get("origin", ""), self.approved_origins)
            databases = origin_state.get("indexedDB", []) or []
            if not isinstance(databases, list):
                raise VaultError("persistent_session_requires_unsupported_indexeddb")
            stack = list(databases)
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    stack.extend(item.keys())
                    stack.extend(item.values())
                elif isinstance(item, list):
                    stack.extend(item)
                elif isinstance(item, str) and \
                        len(item.encode("utf-8")) > self.maximum_value_bytes:
                    raise VaultError("persistent_session_storage_value_too_large")
            result.append({"origin": origin, "indexedDB": databases})
        return sorted(result, key=lambda item: item["origin"])

    def save(
        self, environment_id: str, created_at: str, expires_at: str,
        cookies: list[dict[str, Any]], web_storage: list[dict[str, Any]] | None = None,
        indexed_db: list[dict[str, Any]] | None = None,
        reference_id: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        scope = (TenantScope(tenant_id, workspace_id)
                 if tenant_id is not None or workspace_id is not None
                 else TenantScope.local_development())
        if environment_id != "china_uat":
            raise VaultError("persistent_session_environment_mismatch")
        filtered_cookies = self._approved_cookies(cookies)
        filtered_storage = self._approved_web_storage(web_storage)
        filtered_indexed_db = self._approved_indexed_db(indexed_db)
        item_count = len(filtered_cookies) + sum(
            len(origin[k]) for origin in filtered_storage
            for k in ("local_storage", "session_storage")) + sum(
                len(origin["indexedDB"]) for origin in filtered_indexed_db)
        if item_count == 0 and not any(i["indexedDB"] for i in filtered_indexed_db):
            raise VaultError("persistent_session_empty_authentication_state")
        if item_count > self.maximum_item_count:
            raise VaultError("persistent_session_storage_item_limit_exceeded")
        reference_id = reference_id or f"VAULT-{uuid.uuid4().hex[:24].upper()}"
        payload_object = {
            "schema_version": VAULT_VERSION,
            "environment_id": environment_id,
            "tenant_id": scope.tenant_id,
            "workspace_id": scope.workspace_id,
            "approved_origins": sorted(self.approved_origins),
            "created_at": created_at,
            "expires_at": expires_at,
            "cookies": filtered_cookies,
            "web_storage": filtered_storage,
            "indexed_db": filtered_indexed_db,
        }
        plaintext = bytearray(_canonical(payload_object))
        if len(plaintext) > self.maximum_vault_bytes:
            plaintext[:] = b"\x00" * len(plaintext)
            raise VaultError("persistent_session_vault_too_large")
        metadata = {
            "schema_version": VAULT_VERSION,
            "environment_id": environment_id,
            "tenant_id": scope.tenant_id,
            "workspace_id": scope.workspace_id,
            "created_at": created_at,
            "expires_at": expires_at,
            "approved_origins": sorted(self.approved_origins),
            "payload_sha256": hashlib.sha256(plaintext).hexdigest(),
            "cookie_count": len(filtered_cookies),
            "local_storage_count": sum(
                len(item["local_storage"]) for item in filtered_storage),
            "session_storage_count": sum(
                len(item["session_storage"]) for item in filtered_storage),
            "indexed_db_count": sum(
                len(item["indexedDB"]) for item in filtered_indexed_db),
            "encryption": "AES-256-GCM",
            "key_protection": "DPAPI-CurrentUser",
            "validation_status": "encrypted_saved",
        }
        aad = _canonical(metadata)
        dek = bytearray(os.urandom(32))
        nonce = os.urandom(12)
        try:
            ciphertext = AESGCM(bytes(dek)).encrypt(nonce, bytes(plaintext), aad)
            wrapped_key = self.protector.protect(bytes(dek))
        except VaultError:
            raise
        except Exception as exc:
            raise VaultError("persistent_session_save_failed") from exc
        finally:
            plaintext[:] = b"\x00" * len(plaintext)
            dek[:] = b"\x00" * len(dek)
        envelope = _canonical({
            "schema_version": VAULT_VERSION,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "wrapped_key": base64.b64encode(wrapped_key).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        })
        digest = hashlib.sha256(envelope).hexdigest()
        metadata["ciphertext_sha256"] = digest
        cipher_path, metadata_path = self._paths(reference_id)
        previous_cipher = cipher_path.read_bytes() if cipher_path.exists() else None
        previous_metadata = (
            metadata_path.read_bytes() if metadata_path.exists() else None)
        try:
            self._atomic_write(cipher_path, envelope)
            # Metadata is AAD-authenticated. Replacing it last makes rotation fail closed.
            self._atomic_write(metadata_path, _canonical(metadata))
        except Exception as exc:
            if previous_cipher is not None and previous_metadata is not None:
                self._atomic_write(cipher_path, previous_cipher)
                self._atomic_write(metadata_path, previous_metadata)
            else:
                self.delete(reference_id)
            raise VaultError("persistent_session_save_failed") from exc
        return {"vault_reference_id": reference_id, **metadata}

    def load(self, reference_id: str, environment_id: str, *,
             tenant_id: str | None = None,
             workspace_id: str | None = None) -> dict[str, Any]:
        scope = (TenantScope(tenant_id, workspace_id)
                 if tenant_id is not None or workspace_id is not None
                 else TenantScope.local_development())
        cipher_path, metadata_path = self._paths(reference_id)
        try:
            envelope_bytes = cipher_path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.delete(reference_id)
            raise VaultError("persistent_session_not_found") from exc
        if hashlib.sha256(envelope_bytes).hexdigest() != metadata.get("ciphertext_sha256"):
            self.delete(reference_id)
            raise VaultError("persistent_session_corrupted")
        try:
            envelope = json.loads(envelope_bytes)
        except Exception as exc:
            self.delete(reference_id)
            raise VaultError("persistent_session_corrupted") from exc
        aad_metadata = dict(metadata)
        aad_metadata.pop("ciphertext_sha256", None)
        try:
            wrapped_key = base64.b64decode(envelope["wrapped_key"], validate=True)
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
            dek = bytearray(self.protector.unprotect(wrapped_key))
            plaintext = bytearray(AESGCM(bytes(dek)).decrypt(
                nonce, ciphertext, _canonical(aad_metadata)))
            payload = json.loads(bytes(plaintext))
        except VaultError:
            self.delete(reference_id)
            raise
        except InvalidTag as exc:
            self.delete(reference_id)
            raise VaultError("persistent_session_corrupted") from exc
        except Exception as exc:
            self.delete(reference_id)
            raise VaultError("persistent_session_decryption_failed") from exc
        finally:
            if "dek" in locals():
                dek[:] = b"\x00" * len(dek)
            if "plaintext" in locals():
                plaintext[:] = b"\x00" * len(plaintext)
        if payload.get("environment_id") != environment_id or \
                metadata.get("environment_id") != environment_id:
            self.delete(reference_id)
            raise VaultError("persistent_session_environment_mismatch")
        payload_scope = TenantScope(
            str(payload.get("tenant_id") or TenantScope.local_development().tenant_id),
            str(payload.get("workspace_id") or TenantScope.local_development().workspace_id),
        )
        metadata_scope = TenantScope(
            str(metadata.get("tenant_id") or TenantScope.local_development().tenant_id),
            str(metadata.get("workspace_id") or TenantScope.local_development().workspace_id),
        )
        try:
            scope.assert_same(payload_scope)
            scope.assert_same(metadata_scope)
        except ValueError as exc:
            raise VaultError("persistent_session_tenant_scope_mismatch") from exc
        if payload.get("approved_origins") != sorted(self.approved_origins) or \
                metadata.get("approved_origins") != sorted(self.approved_origins):
            self.delete(reference_id)
            raise VaultError("persistent_session_origin_not_allowed")
        if payload.get("schema_version") != VAULT_VERSION or \
                metadata.get("schema_version") != VAULT_VERSION or \
                metadata.get("payload_sha256") != hashlib.sha256(
                    _canonical(payload)).hexdigest():
            self.delete(reference_id)
            raise VaultError("persistent_session_corrupted")
        try:
            expires = datetime.fromisoformat(
                payload["expires_at"].replace("Z", "+00:00"))
        except Exception as exc:
            self.delete(reference_id)
            raise VaultError("persistent_session_corrupted") from exc
        if expires <= datetime.now(timezone.utc):
            self.delete(reference_id)
            raise VaultError("persistent_session_expired")
        return payload

    def replace(
        self, reference_id: str, environment_id: str, created_at: str,
        expires_at: str, cookies: list[dict[str, Any]],
        web_storage: list[dict[str, Any]] | None = None,
        indexed_db: list[dict[str, Any]] | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
    ) -> dict[str, Any]:
        self.load(reference_id, environment_id, tenant_id=tenant_id,
                  workspace_id=workspace_id)
        return self.save(
            environment_id, created_at, expires_at, cookies, web_storage,
            indexed_db, reference_id=reference_id, tenant_id=tenant_id,
            workspace_id=workspace_id)

    def validate(self, reference_id: str, environment_id: str, *,
                 tenant_id: str | None = None,
                 workspace_id: str | None = None) -> dict[str, Any]:
        payload = self.load(reference_id, environment_id, tenant_id=tenant_id,
                            workspace_id=workspace_id)
        return {
            "status": "valid",
            "schema_version": payload["schema_version"],
            "environment_id": payload["environment_id"],
            "tenant_id": payload.get("tenant_id", TenantScope.local_development().tenant_id),
            "workspace_id": payload.get("workspace_id", TenantScope.local_development().workspace_id),
            "approved_origins": payload["approved_origins"],
            "created_at": payload["created_at"],
            "expires_at": payload["expires_at"],
            "storage_types": sorted([
                *(["cookies"] if payload["cookies"] else []),
                *(["localStorage"] if any(
                    i["local_storage"] for i in payload["web_storage"]) else []),
                *(["sessionStorage"] if any(
                    i["session_storage"] for i in payload["web_storage"]) else []),
                *(["IndexedDB"] if any(
                    i["indexedDB"] for i in payload["indexed_db"]) else []),
            ]),
        }

    def inspect_metadata(
        self, reference_id: str, environment_id: str,
        *, tenant_id: str | None = None, workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate public metadata without decrypting or changing vault state."""
        cipher_path, metadata_path = self._paths(reference_id)
        try:
            envelope_bytes = cipher_path.read_bytes()
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise VaultError("persistent_session_not_found") from exc
        if hashlib.sha256(envelope_bytes).hexdigest() != metadata.get(
                "ciphertext_sha256"):
            raise VaultError("persistent_session_corrupted")
        if metadata.get("environment_id") != environment_id:
            raise VaultError("persistent_session_environment_mismatch")
        scope = (TenantScope(tenant_id, workspace_id)
                 if tenant_id is not None or workspace_id is not None
                 else TenantScope.local_development())
        metadata_scope = TenantScope(
            str(metadata.get("tenant_id") or TenantScope.local_development().tenant_id),
            str(metadata.get("workspace_id") or TenantScope.local_development().workspace_id),
        )
        try:
            scope.assert_same(metadata_scope)
        except ValueError as exc:
            raise VaultError("persistent_session_tenant_scope_mismatch") from exc
        if metadata.get("approved_origins") != sorted(self.approved_origins):
            raise VaultError("persistent_session_origin_not_allowed")
        if metadata.get("schema_version") != VAULT_VERSION:
            raise VaultError("persistent_session_corrupted")
        try:
            expires = datetime.fromisoformat(
                str(metadata["expires_at"]).replace("Z", "+00:00"))
        except Exception as exc:
            raise VaultError("persistent_session_corrupted") from exc
        if expires <= datetime.now(timezone.utc):
            raise VaultError("persistent_session_expired")
        return {
            "status": "metadata_valid",
            "schema_version": metadata["schema_version"],
            "environment_id": metadata["environment_id"],
            "tenant_id": metadata.get("tenant_id", TenantScope.local_development().tenant_id),
            "workspace_id": metadata.get("workspace_id", TenantScope.local_development().workspace_id),
            "approved_origins": metadata["approved_origins"],
            "created_at": metadata["created_at"],
            "expires_at": metadata["expires_at"],
            "storage_types": sorted([
                *(["cookies"] if int(metadata.get("cookie_count", 0)) else []),
                *(["localStorage"] if int(
                    metadata.get("local_storage_count", 0)) else []),
                *(["sessionStorage"] if int(
                    metadata.get("session_storage_count", 0)) else []),
                *(["IndexedDB"] if int(
                    metadata.get("indexed_db_count", 0)) else []),
            ]),
        }

    def delete(self, reference_id: str):
        try:
            paths = self._paths(reference_id)
        except VaultError:
            return
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def exists(self, reference_id: str) -> bool:
        return all(path.exists() for path in self._paths(reference_id))
