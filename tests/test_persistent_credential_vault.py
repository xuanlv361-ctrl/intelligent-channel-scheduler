from pathlib import Path

import pytest

from backend.persistent_credential_vault import (
    CredentialScope, CredentialVaultError, PersistentCredentialVault,
)


class FakeProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"FAKE-DPAPI:" + bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"FAKE-DPAPI:"):
            raise CredentialVaultError("credential_decryption_failed")
        return bytes(value ^ 0xA5 for value in ciphertext[11:])


def scope(principal="windows-user-a", tenant="tenant-a", workspace="workspace-a",
          environment="china_uat"):
    return CredentialScope(principal, tenant, workspace, environment)


def vault(directory: Path):
    return PersistentCredentialVault(directory, FakeProtector())


def test_restart_recovers_encrypted_credential_without_plaintext(tmp_path):
    secret = "test-only-persistent-credential-not-real"
    first = vault(tmp_path / "credentials")
    metadata = first.save(scope(), secret)
    files = list((tmp_path / "credentials").glob("*.uatkey"))
    assert len(files) == 1
    assert secret.encode() not in files[0].read_bytes()
    assert b"windows-user-a" not in files[0].read_bytes()
    second = vault(tmp_path / "credentials")
    restored = second.load(scope(), required=True)
    assert restored and restored[0] == secret
    assert restored[1]["credential_source"] == "windows_encrypted_vault"
    assert restored[1]["expires_at"] is None
    assert metadata["key_protection"] == "DPAPI-CurrentUser"


def test_replacement_is_atomic_and_preserves_created_at(tmp_path):
    store = vault(tmp_path)
    original = store.save(scope(), "first-test-key-not-real")
    replacement = store.save(scope(), "second-test-key-not-real")
    assert store.load(scope(), required=True)[0] == "second-test-key-not-real"
    assert replacement["created_at"] == original["created_at"]
    assert replacement["key_fingerprint"] != original["key_fingerprint"]


def test_scope_isolation_and_principal_logout_clear(tmp_path):
    store = vault(tmp_path)
    store.save(scope(environment="china_uat"), "china-key-not-real")
    store.save(scope(environment="overseas"), "overseas-key-not-real")
    store.save(scope(principal="windows-user-b"), "other-user-key-not-real")
    assert store.load(scope(tenant="tenant-b")) is None
    assert store.load(scope(workspace="workspace-b")) is None
    assert store.delete_principal(
        "windows-user-a", "tenant-a", "workspace-a", ("china_uat", "overseas")) == 2
    assert store.load(scope()) is None
    assert store.load(scope(environment="overseas")) is None
    assert store.load(scope(principal="windows-user-b"), required=True)


def test_tampering_fails_closed_without_leaking_secret(tmp_path):
    store = vault(tmp_path)
    store.save(scope(), "tamper-test-key-not-real")
    path = next(tmp_path.glob("*.uatkey"))
    content = bytearray(path.read_bytes())
    content[-8] ^= 1
    path.write_bytes(content)
    with pytest.raises(CredentialVaultError) as exc:
        store.load(scope(), required=True)
    assert "tamper-test-key-not-real" not in str(exc.value)


@pytest.mark.parametrize("value", ["", "bad\nkey", "bad\rkey", "non-ascii-密钥"])
def test_invalid_credentials_are_rejected(value, tmp_path):
    with pytest.raises(CredentialVaultError):
        vault(tmp_path).save(scope(), value)
