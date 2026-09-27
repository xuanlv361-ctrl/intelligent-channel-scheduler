from datetime import timedelta
import json

import backend.app as application
from backend.credential_store import SessionCredentialStore, utcnow
from backend.persistent_credential_vault import PersistentCredentialVault
from tests.test_persistent_credential_vault import FakeProtector
from tests.enterprise_test_client import authenticated_test_client

ORIGIN={"Origin":"http://127.0.0.1:5173","Content-Type":"application/json"}
SECRET="test-only-credential-not-real"
BODY={"api_key":SECRET,"confirmation":{"confirmed":True,"text":"I authorize encrypted persistence of this UAT key for this Windows user until replacement or logout."}}


def fresh_client(monkeypatch, tmp_path):
    monkeypatch.setattr(application,"SESSION_CREDENTIALS",SessionCredentialStore(30,20))
    monkeypatch.setattr(application,"PERSISTENT_CREDENTIALS",
                        PersistentCredentialVault(tmp_path/"credentials",FakeProtector()))
    monkeypatch.setattr(application,"PERSISTENT_CREDENTIAL_ERROR",None)
    monkeypatch.setattr(application.STORE,"path",tmp_path/"credential.sqlite3")
    return authenticated_test_client(
        application.app,runtime=application.ENTERPRISE_HTTP,
        roles=("domestic_uat_operator",),base_url="http://127.0.0.1:8000")


def test_submit_is_identity_scoped_redacted_persistent_and_clear(monkeypatch,tmp_path):
    first=fresh_client(monkeypatch,tmp_path)
    second=authenticated_test_client(
        application.app,runtime=application.ENTERPRISE_HTTP,
        roles=("domestic_uat_operator",),base_url="http://127.0.0.1:8000")
    saved=first.post("/api/v1/uat/credentials/session",headers=ORIGIN,json=BODY)
    assert saved.status_code==200 and SECRET not in saved.text
    assert saved.json()["credential_source"]=="windows_encrypted_vault"
    assert first.get("/api/v1/uat/credentials/status").json()["credential_source"]=="windows_encrypted_vault"
    assert second.get("/api/v1/uat/credentials/status").json()["credential_source"]=="none"
    assert SECRET not in (tmp_path/"credential.sqlite3").read_bytes().decode(errors="ignore")
    assert all(SECRET.encode() not in path.read_bytes()
               for path in (tmp_path/"credentials").glob("*"))
    # A fresh vault object simulates a backend restart without changing identity.
    monkeypatch.setattr(application,"PERSISTENT_CREDENTIALS",
                        PersistentCredentialVault(tmp_path/"credentials",FakeProtector()))
    assert first.get("/api/v1/uat/credentials/status").json()["configured"] is True
    cleared=first.request("DELETE","/api/v1/uat/credentials/session",headers=ORIGIN,json={})
    assert cleared.status_code==200
    assert first.get("/api/v1/uat/credentials/status").json()["credential_source"]!="session"


def test_validation_origin_confirmation_and_key_rules(monkeypatch,tmp_path):
    client=fresh_client(monkeypatch,tmp_path)
    assert client.raw_request("POST","/api/v1/uat/credentials/session",json=BODY).status_code==403
    assert client.post("/api/v1/uat/credentials/session",headers=ORIGIN,json={**BODY,"api_key":"bad\nkey"}).status_code==400
    assert client.post("/api/v1/uat/credentials/session",headers=ORIGIN,json={**BODY,"confirmation":{"confirmed":False}}).status_code==400
    assert client.post("/api/v1/uat/credentials/session",headers=ORIGIN,json={**BODY,"api_key":"x"*4097}).status_code==400


def test_ttl_environment_fallback_and_precedence(monkeypatch,tmp_path):
    store=SessionCredentialStore(1,2)
    sid,_=store.create(SECRET,utcnow()-timedelta(minutes=2))
    assert store.resolve(sid)==(None,None)
    client=fresh_client(monkeypatch,tmp_path)
    monkeypatch.setenv("WEIMETA_UAT_API_KEY","environment-test-only")
    assert client.get("/api/v1/uat/credentials/status").json()["credential_source"]=="environment"
    client.post("/api/v1/uat/credentials/session",headers=ORIGIN,json=BODY)
    assert client.get("/api/v1/uat/credentials/status").json()["credential_source"]=="windows_encrypted_vault"


def test_connection_test_is_models_only_and_does_not_enable_execution(monkeypatch,tmp_path):
    client=fresh_client(monkeypatch,tmp_path)
    state_before=client.get("/api/v1/environments/china_uat/execution-state").json()
    calls=[]
    def transport(url,key,timeout):
        calls.append((url,key))
        return {"status":200,"body":json.dumps({"data":[]}).encode()}
    monkeypatch.setattr(application,"MODELS_TRANSPORT",transport)
    client.post("/api/v1/uat/credentials/session",headers=ORIGIN,json=BODY)
    result=client.post("/api/v1/uat/credentials/test",headers=ORIGIN,json={})
    assert result.json()["connection_status"]=="success"
    assert calls==[("https://api-uat.weimeta.cn/v1/models",SECRET)]
    assert "chat/completions" not in json.dumps(calls)
    state_after=client.get("/api/v1/environments/china_uat/execution-state").json()
    assert state_after==state_before


def test_application_logout_deletes_persistent_key_before_revoking_session(monkeypatch,tmp_path):
    client=fresh_client(monkeypatch,tmp_path)
    saved=client.post("/api/v1/uat/credentials/session",headers=ORIGIN,json=BODY)
    assert saved.status_code==200
    assert len(list((tmp_path/"credentials").glob("*.uatkey")))==1
    logged_out=client.post("/api/v1/security/session/logout",headers=ORIGIN,json={})
    assert logged_out.status_code==200
    assert list((tmp_path/"credentials").glob("*.uatkey"))==[]
    assert client.get("/api/v1/uat/credentials/status").status_code==401
