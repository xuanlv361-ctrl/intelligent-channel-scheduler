import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.encrypted_session_vault import EncryptedSessionVault, VaultError
from backend.persistent_session_service import PersistentSessionError
from collector.persistent_browser_session import (
    _capture_authentication_state, _web_storage_init_script,
)
from tests.test_persistent_session_mode import FakeDPAPI, cookie, setup


def times():
    now = datetime.now(timezone.utc)
    return now.isoformat(), (now + timedelta(hours=8)).isoformat()


def storage(local=None, session=None, origin="https://uat.weimeta.cn"):
    return [{
        "origin": origin,
        "local_storage": [
            {"name": name, "value": value} for name, value in (local or {}).items()],
        "session_storage": [
            {"name": name, "value": value} for name, value in (session or {}).items()],
    }]


@pytest.mark.parametrize(
    ("local", "session", "expected"),
    [
        ({"local-auth": "synthetic-local-secret"}, {}, ["cookies", "localStorage"]),
        ({}, {"session-auth": "synthetic-session-secret"}, ["cookies", "sessionStorage"]),
        (
            {"local-auth": "synthetic-local-secret"},
            {"session-auth": "synthetic-session-secret"},
            ["cookies", "localStorage", "sessionStorage"],
        ),
    ],
)
def test_encrypted_web_storage_round_trip(tmp_path, local, session, expected):
    vault = EncryptedSessionVault(tmp_path / "vault", FakeDPAPI())
    created, expires = times()
    metadata = vault.save(
        "china_uat", created, expires, [cookie()], storage(local, session))
    disk = b"".join(path.read_bytes() for path in vault.directory.iterdir())
    assert b"synthetic-local-secret" not in disk
    assert b"synthetic-session-secret" not in disk
    payload = vault.load(metadata["vault_reference_id"], "china_uat")
    assert payload["web_storage"][0]["local_storage"] == [
        {"name": key, "value": value} for key, value in local.items()]
    assert payload["web_storage"][0]["session_storage"] == [
        {"name": key, "value": value} for key, value in session.items()]
    assert vault.validate(
        metadata["vault_reference_id"], "china_uat")["storage_types"] == expected


@pytest.mark.parametrize("origin", [
    "http://uat.weimeta.cn",
    "https://uat.weimeta.cn.evil.test",
    "https://localhost",
    "https://user:secret@uat.weimeta.cn",
    "https://uat.weimeta.cn?token=secret",
])
def test_web_storage_requires_exact_approved_origin(tmp_path, origin):
    vault = EncryptedSessionVault(tmp_path / "vault", FakeDPAPI())
    created, expires = times()
    with pytest.raises(VaultError, match="origin_not_allowed"):
        vault.save(
            "china_uat", created, expires, [cookie()],
            storage({"auth": "synthetic"}, origin=origin))


def test_metadata_tampering_is_authenticated(tmp_path):
    vault = EncryptedSessionVault(tmp_path / "vault", FakeDPAPI())
    created, expires = times()
    metadata = vault.save(
        "china_uat", created, expires, [cookie()], storage({"auth": "synthetic"}))
    path = next(vault.directory.glob("*.metadata.json"))
    changed = json.loads(path.read_text(encoding="utf-8"))
    changed["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(VaultError, match="corrupted"):
        vault.load(metadata["vault_reference_id"], "china_uat")


def test_storage_limits_and_key_allowlist(tmp_path):
    created, expires = times()
    vault = EncryptedSessionVault(
        tmp_path / "vault", FakeDPAPI(), maximum_value_bytes=8,
        allowed_storage_keys={"approved"})
    with pytest.raises(VaultError, match="key_not_allowed"):
        vault.save(
            "china_uat", created, expires, [cookie(value="short")],
            storage({"unknown": "short"}))
    with pytest.raises(VaultError, match="value_too_large"):
        vault.save(
            "china_uat", created, expires, [cookie(value="short")],
            storage({"approved": "longer-than-eight"}))


def test_diagnostic_and_sqlite_never_store_values(tmp_path):
    service, _ = setup(tmp_path)
    pairing = service.start_pairing("china_uat", 8, True)
    diagnostic = {
        "cookie_metadata": [{
            "name": "session", "domain": "uat.weimeta.cn", "secure": True,
            "sameSite": "Lax", "expires": -1,
        }],
        "local_storage_keys": ["local-auth"],
        "session_storage_keys": ["session-auth"],
        "indexed_db_names": [],
        "authentication_origins": ["https://uat.weimeta.cn"],
        "changed_storage_types": ["cookies", "localStorage", "sessionStorage"],
        "indexed_db_capture_supported": True,
    }
    public = service.save_storage_diagnostic(pairing["pairing_id"], diagnostic)
    assert public["diagnostic"]["local_storage_keys"] == ["local-auth"]
    database = service.realtime.database_path.read_bytes()
    assert b"synthetic-local-secret" not in database
    assert b"synthetic-session-secret" not in database


def test_atomic_rotation_restores_previous_ciphertext_on_write_failure(
    tmp_path, monkeypatch,
):
    vault = EncryptedSessionVault(tmp_path / "vault", FakeDPAPI())
    created, expires = times()
    metadata = vault.save(
        "china_uat", created, expires, [cookie(value="old-secret")],
        storage({"auth": "old-storage"}))
    before = {
        path.name: path.read_bytes() for path in vault.directory.iterdir()}
    original = vault._atomic_write
    failed = False

    def fail_metadata_once(path, content):
        nonlocal failed
        if path.name.endswith(".metadata.json") and not failed:
            failed = True
            raise OSError("synthetic atomic failure")
        original(path, content)

    monkeypatch.setattr(vault, "_atomic_write", fail_metadata_once)
    with pytest.raises(VaultError, match="save_failed"):
        vault.replace(
            metadata["vault_reference_id"], "china_uat", created, expires,
            [cookie(value="new-secret")], storage({"auth": "new-storage"}), [])
    after = {path.name: path.read_bytes() for path in vault.directory.iterdir()}
    assert after == before
    assert vault.load(
        metadata["vault_reference_id"], "china_uat")["cookies"][0]["value"] == \
        "old-secret"


def test_init_script_is_exact_origin_guarded():
    script = _web_storage_init_script(storage({"auth": "synthetic"}))
    assert "location.origin" in script
    assert "state.local_storage" in script
    assert "state.session_storage" in script
    assert "http://" not in script


def test_indexeddb_blocks_when_official_playwright_capture_is_unavailable():
    class Context:
        def storage_state(self):
            return {}

        def cookies(self, origins):
            return []

    class Page:
        url = "https://uat.weimeta.cn/console/billing/logs"

        def evaluate(self, script):
            if "Object.entries" in script:
                return {"local": [], "session": []}
            return {"local": [], "session": [], "indexed": ["synthetic-auth-db"]}

    with pytest.raises(
            PersistentSessionError, match="requires_unsupported_indexeddb"):
        _capture_authentication_state(Context(), Page())


def test_explicit_no_indexeddb_capture_does_not_scan_whole_chrome_profile():
    class Context:
        def storage_state(self, indexed_db=False):
            raise AssertionError("whole profile must not be scanned")

        def cookies(self, origins):
            assert origins
            return []

    class Page:
        url = "https://uat.weimeta.cn/console/billing/logs"

        @staticmethod
        def evaluate(_script):
            return {"local": [], "session": []}

    cookies, web_storage, indexed_db = _capture_authentication_state(
        Context(), Page(), capture_indexed_db=False)
    assert cookies == []
    assert web_storage == [{
        "origin": "https://uat.weimeta.cn",
        "local_storage": [], "session_storage": [],
    }]
    assert indexed_db == []
