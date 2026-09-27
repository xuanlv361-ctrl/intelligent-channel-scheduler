"""Tenant-scoped external Host control plane for the Formal Agent Skill runtime."""
from __future__ import annotations

import ipaddress
import json
import socket
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, urljoin, urlsplit

from backend.agent_skill_host_credential_vault import AgentSkillHostCredentialVault
from backend.tenant_security import TenantScope


class AgentSkillHostError(RuntimeError):
    def __init__(self, code: str, **safe_context: Any) -> None:
        super().__init__(code)
        self.code = code
        self.safe_context = redact(safe_context)


@dataclass(frozen=True)
class HostTransportResponse:
    status_code: int
    payload: Any
    headers: Mapping[str, str] | None = None
    latency_ms: int | None = None
    tls_verified: bool = True


class HostTransport(Protocol):
    def request(self, *, method: str, url: str, headers: Mapping[str, str],
                json_body: Any | None, timeout_seconds: int,
                follow_redirects: bool) -> HostTransportResponse: ...


class StandardHostTransport:
    """Small HTTPS JSON transport with redirects disabled and bounded bodies."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def request(self, *, method: str, url: str, headers: Mapping[str, str],
                json_body: Any | None, timeout_seconds: int,
                follow_redirects: bool) -> HostTransportResponse:
        body = None if json_body is None else _json(json_body).encode("utf-8")
        safe_headers = dict(headers)
        if body is not None:
            safe_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=safe_headers)
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            self._NoRedirect())
        started = time.monotonic()
        try:
            response = opener.open(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as exc:
            response = exc
        raw = response.read(1_048_577)
        if len(raw) > 1_048_576:
            raise AgentSkillHostError("host_protocol_incompatible")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentSkillHostError("host_protocol_incompatible") from exc
        return HostTransportResponse(
            status_code=int(response.status), payload=payload,
            headers={str(k).lower(): str(v) for k, v in response.headers.items()},
            latency_ms=int((time.monotonic() - started) * 1000),
            tls_verified=urlsplit(url).scheme == "https")


SENSITIVE_KEYS = frozenset({
    "authorization", "cookie", "set-cookie", "token", "access_token",
    "refresh_token", "api_key", "apikey", "secret", "client_secret", "credential",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): "<redacted>" if str(k).casefold().replace("-", "_") in SENSITIVE_KEYS
                else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


class AgentSkillHostService:
    HOST_STATES = frozenset({"not_configured", "pending_test", "testing", "connected",
                             "authentication_failed", "network_failed",
                             "protocol_incompatible", "disabled"})
    PROTOCOLS = frozenset({"formal-agent-skill", "mcp"})

    @staticmethod
    def _auth_type(value: str) -> str:
        return "bearer" if value == "bearer_token" else value

    def __init__(self, database_path: str | Path, *, scope: TenantScope | None = None,
                 vault: AgentSkillHostCredentialVault | None = None,
                 transport: HostTransport | None = None,
                 allowed_hosts: set[str] | frozenset[str] | None = None,
                 allow_loopback_http: bool = False,
                 loopback_ports: set[int] | frozenset[int] | None = None,
                 request_timeout_seconds: int = 30,
                 dns_resolver: Callable[..., Any] = socket.getaddrinfo) -> None:
        self.path = Path(database_path)
        self.scope = scope or TenantScope.local_development()
        self.vault = vault or AgentSkillHostCredentialVault(self.scope)
        self.transport = transport or StandardHostTransport()
        self.allowed_hosts = frozenset(str(h).casefold() for h in
            (allowed_hosts or {"api-uat.weimeta.cn"}))
        self.allow_loopback_http = bool(allow_loopback_http)
        self.loopback_ports = frozenset(loopback_ports or {8010})
        self.request_timeout_seconds = min(max(int(request_timeout_seconds), 1), 30)
        self.dns_resolver = dns_resolver
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS formal_agent_skill_hosts(
              host_id TEXT PRIMARY KEY,host_name TEXT NOT NULL,host_type TEXT NOT NULL,base_url TEXT,
              enabled INTEGER NOT NULL DEFAULT 0,connection_status TEXT NOT NULL DEFAULT 'not_tested',
              last_tested_at TEXT,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL)""")
            existing = {row[1] for row in db.execute("PRAGMA table_info(formal_agent_skill_hosts)")}
            additions = {
                "environment":"TEXT NOT NULL DEFAULT 'china_uat'", "protocol":"TEXT NOT NULL DEFAULT 'formal-agent-skill'",
                "protocol_version":"TEXT NOT NULL DEFAULT '1.0'", "auth_type":"TEXT NOT NULL DEFAULT 'none'",
                "secret_ref":"TEXT", "credential_fingerprint":"TEXT", "capabilities_json":"TEXT NOT NULL DEFAULT '[]'",
                "last_test_result_json":"TEXT", "last_invoked_at":"TEXT", "created_at":"TEXT", "updated_at":"TEXT",
            }
            for name, declaration in additions.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE formal_agent_skill_hosts ADD COLUMN {name} {declaration}")
            db.execute("""UPDATE formal_agent_skill_hosts SET connection_status='pending_test'
              WHERE connection_status='not_tested'""")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS formal_agent_skill_host_installations(
              installation_id TEXT PRIMARY KEY,host_id TEXT NOT NULL,skill_id TEXT NOT NULL,
              skill_version TEXT NOT NULL,status TEXT NOT NULL,installed_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
              UNIQUE(host_id,skill_id,tenant_id,workspace_id));
            CREATE TABLE IF NOT EXISTS formal_agent_skill_host_invocations(
              invocation_id TEXT PRIMARY KEY,audit_id TEXT NOT NULL,host_id TEXT NOT NULL,
              skill_id TEXT NOT NULL,skill_version TEXT NOT NULL,operator_id TEXT NOT NULL,
              permission_decision TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT NOT NULL,
              latency_ms INTEGER NOT NULL,result TEXT NOT NULL,network_called INTEGER NOT NULL,
              write_occurred INTEGER NOT NULL,request_id TEXT,decision_id TEXT,safe_result_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS formal_agent_skill_host_audit(
              audit_id TEXT PRIMARY KEY,host_id TEXT,operation TEXT NOT NULL,operator_id TEXT NOT NULL,
              result TEXT NOT NULL,error_code TEXT,safe_detail_json TEXT NOT NULL,created_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_skill_hosts_scope ON formal_agent_skill_hosts(tenant_id,workspace_id);
            CREATE INDEX IF NOT EXISTS idx_skill_host_inv_scope ON formal_agent_skill_host_invocations(tenant_id,workspace_id,host_id);
            CREATE INDEX IF NOT EXISTS idx_skill_host_audit_scope ON formal_agent_skill_host_audit(tenant_id,workspace_id,host_id);
            """)
            self._deduplicate_host_registry(db)

    def _deduplicate_host_registry(self, db: sqlite3.Connection) -> None:
        """Merge accidental duplicate Host registrations without losing evidence.

        The canonical identity intentionally includes protocol version so an
        incompatible-protocol test remains a separate, truthful Host record.
        """
        rows = db.execute("""SELECT * FROM formal_agent_skill_hosts
          ORDER BY tenant_id,workspace_id,base_url,host_type,protocol,protocol_version,
                   enabled DESC,(connection_status='connected') DESC,updated_at DESC,created_at DESC""").fetchall()
        groups: dict[tuple[str, ...], list[sqlite3.Row]] = {}
        for row in rows:
            key = tuple(str(row[name] or "") for name in (
                "tenant_id", "workspace_id", "base_url", "host_type", "protocol", "protocol_version"))
            groups.setdefault(key, []).append(row)
        for duplicates in groups.values():
            if len(duplicates) < 2 or not duplicates[0]["base_url"]:
                continue
            keeper = duplicates[0]
            merged = 0
            for duplicate in duplicates[1:]:
                duplicate_id = str(duplicate["host_id"])
                keeper_id = str(keeper["host_id"])
                installations = db.execute(
                    "SELECT * FROM formal_agent_skill_host_installations WHERE host_id=?", (duplicate_id,)).fetchall()
                for installation in installations:
                    current = db.execute("""SELECT * FROM formal_agent_skill_host_installations
                      WHERE host_id=? AND skill_id=? AND tenant_id=? AND workspace_id=?""",
                      (keeper_id, installation["skill_id"], installation["tenant_id"],
                       installation["workspace_id"])).fetchone()
                    if current is None:
                        db.execute("UPDATE formal_agent_skill_host_installations SET host_id=? WHERE installation_id=?",
                                   (keeper_id, installation["installation_id"]))
                    else:
                        if str(installation["updated_at"]) > str(current["updated_at"]):
                            db.execute("""UPDATE formal_agent_skill_host_installations
                              SET skill_version=?,status=?,updated_at=? WHERE installation_id=?""",
                              (installation["skill_version"], installation["status"],
                               installation["updated_at"], current["installation_id"]))
                        db.execute("DELETE FROM formal_agent_skill_host_installations WHERE installation_id=?",
                                   (installation["installation_id"],))
                db.execute("UPDATE formal_agent_skill_host_invocations SET host_id=? WHERE host_id=?",
                           (keeper_id, duplicate_id))
                db.execute("UPDATE formal_agent_skill_host_audit SET host_id=? WHERE host_id=?",
                           (keeper_id, duplicate_id))
                try:
                    self.vault.delete(duplicate_id)
                except Exception:
                    # A missing or already-revoked encrypted envelope is not a
                    # reason to preserve a duplicate registry row.
                    pass
                db.execute("DELETE FROM formal_agent_skill_hosts WHERE host_id=?", (duplicate_id,))
                merged += 1
            if merged:
                self._audit(db, str(keeper["host_id"]), "deduplicate", "runtime-migration", "success",
                            detail={"merged_count": merged, "canonical_identity": "address_protocol"})

    def _validate_url(self, value: str) -> str:
        try:
            parsed = urlsplit(str(value))
            port = parsed.port
        except ValueError as exc:
            raise AgentSkillHostError("host_url_invalid") from exc
        hostname = (parsed.hostname or "").casefold()
        is_loopback = hostname in {"127.0.0.1", "localhost", "::1"}
        local_http = (self.allow_loopback_http and is_loopback
                      and parsed.scheme == "http" and port in self.loopback_ports)
        public_https = parsed.scheme == "https" and port in {None, 443}
        if ((not local_http and not public_https) or not hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise AgentSkillHostError("host_url_invalid")
        if hostname not in self.allowed_hosts:
            raise AgentSkillHostError("host_url_not_allowed")
        try:
            addresses = {item[4][0] for item in self.dns_resolver(
                hostname, port or (8010 if local_http else 443), type=socket.SOCK_STREAM)}
        except OSError as exc:
            raise AgentSkillHostError("host_dns_resolution_failed") from exc
        if not addresses:
            raise AgentSkillHostError("host_dns_resolution_failed")
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not ip.is_global and not (local_http and ip.is_loopback):
                raise AgentSkillHostError("host_address_not_allowed")
        normalized_path = "/" + "/".join(segment for segment in parsed.path.split("/") if segment)
        authority = hostname if port is None else f"{hostname}:{port}"
        return f"{parsed.scheme}://{authority}{'' if normalized_path == '/' else normalized_path}"

    def _host(self, db: sqlite3.Connection, host_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM formal_agent_skill_hosts WHERE host_id=? AND tenant_id=? AND workspace_id=?",
                         (host_id, *self.scope.sql_parameters())).fetchone()
        if not row:
            raise AgentSkillHostError("host_not_configured")
        return row

    def _audit(self, db: sqlite3.Connection, host_id: str | None, operation: str,
               operator_id: str, result: str, error_code: str | None = None,
               detail: Any | None = None, audit_id: str | None = None) -> str:
        audit_id = audit_id or f"FSHA-{uuid.uuid4()}"
        db.execute("""INSERT INTO formal_agent_skill_host_audit VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (audit_id,host_id,operation,operator_id,result,error_code,
                    _json(redact(detail or {})),_now(),*self.scope.sql_parameters()))
        return audit_id

    def _serialize(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["status"] = item.pop("connection_status")
        item["capabilities"] = json.loads(item.pop("capabilities_json") or "[]")
        item["last_test_result"] = json.loads(item.pop("last_test_result_json") or "null")
        item.pop("secret_ref", None)
        count = db.execute("""SELECT COUNT(*) FROM formal_agent_skill_host_installations
          WHERE host_id=? AND status='installed' AND tenant_id=? AND workspace_id=?""",
          (row["host_id"],*self.scope.sql_parameters())).fetchone()[0]
        item["installed_skill_count"] = count
        return item

    def create_host(self, *, host_name: str, host_type: str, base_url: str,
                    environment: str = "china_uat", protocol: str = "formal-agent-skill",
                    protocol_version: str = "1.0", auth_type: str = "none",
                    operator_id: str = "local_operator") -> dict[str, Any]:
        if not host_name.strip() or len(host_name) > 128 or protocol not in self.PROTOCOLS:
            raise AgentSkillHostError("host_input_invalid")
        safe_url = self._validate_url(base_url) if base_url.strip() else ""
        auth_type = self._auth_type(auth_type)
        if auth_type not in self.vault.SUPPORTED_AUTH:
            raise AgentSkillHostError("host_auth_type_invalid")
        host_id, now = f"FSH-{uuid.uuid4()}", _now()
        with self.connect() as db:
            db.execute("""INSERT INTO formal_agent_skill_hosts(
              host_id,host_name,host_type,base_url,enabled,connection_status,last_tested_at,
              tenant_id,workspace_id,environment,protocol,protocol_version,auth_type,secret_ref,
              credential_fingerprint,capabilities_json,last_test_result_json,last_invoked_at,created_at,updated_at)
              VALUES(?,?,?,?,0,?,NULL,?,?,?,?,?,?,NULL,NULL,'[]',NULL,NULL,?,?)""",
              (host_id,host_name.strip(),host_type,safe_url,
               "pending_test" if safe_url else "not_configured",*self.scope.sql_parameters(),environment,
               protocol,protocol_version,auth_type,now,now))
            self._audit(db,host_id,"create",operator_id,"success",detail={"base_url":safe_url})
            return self._serialize(db,self._host(db,host_id))

    def list_hosts(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM formal_agent_skill_hosts WHERE tenant_id=? AND workspace_id=?
              ORDER BY created_at,host_name""",self.scope.sql_parameters()).fetchall()
            items = [self._serialize(db,row) for row in rows]
            return {"items":items,"count":len(items),
                    "connected_count":sum(item["status"]=="connected" and item["enabled"] for item in items),
                    "external_host_status":"connected" if any(item["status"]=="connected" for item in items)
                                           else ("pending_test" if items else "not_configured")}

    def get_host(self, host_id: str) -> dict[str, Any]:
        with self.connect() as db:
            return self._serialize(db,self._host(db,host_id))

    def update_host(self, host_id: str, *, operator_id: str = "local_operator", **changes: Any) -> dict[str, Any]:
        allowed = {"host_name","host_type","base_url","environment","protocol","protocol_version","auth_type"}
        if not changes or set(changes)-allowed:
            raise AgentSkillHostError("host_input_invalid")
        if "base_url" in changes:
            raw_url = str(changes["base_url"] or "")
            changes["base_url"] = self._validate_url(raw_url) if raw_url.strip() else ""
        if "protocol" in changes and changes["protocol"] not in self.PROTOCOLS:
            raise AgentSkillHostError("host_protocol_incompatible")
        if "auth_type" in changes and changes["auth_type"] not in self.vault.SUPPORTED_AUTH:
            changes["auth_type"] = self._auth_type(str(changes["auth_type"]))
            if changes["auth_type"] not in self.vault.SUPPORTED_AUTH:
                raise AgentSkillHostError("host_auth_type_invalid")
        with self.connect() as db:
            before = self._host(db,host_id)
            assignments = ",".join(f"{key}=?" for key in changes)
            next_status = "not_configured" if "base_url" in changes and not changes["base_url"] else "pending_test"
            db.execute(f"UPDATE formal_agent_skill_hosts SET {assignments},connection_status=?,enabled=0,updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?",
                       (*changes.values(),next_status,_now(),host_id,*self.scope.sql_parameters()))
            self._audit(db,host_id,"update",operator_id,"success",detail={"fields":sorted(changes)})
            return self._serialize(db,self._host(db,host_id))

    def set_credentials(self, host_id: str, credential: str | dict[str, Any] | None, *,
                        auth_type: str | None = None, operator_id: str = "local_operator") -> dict[str, Any]:
        with self.connect() as db:
            host = self._host(db,host_id)
            selected = self._auth_type(auth_type or host["auth_type"])
            metadata = self.vault.save(host_id,selected,credential)
            db.execute("""UPDATE formal_agent_skill_hosts SET auth_type=?,secret_ref=?,credential_fingerprint=?,
              connection_status='pending_test',enabled=0,updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?""",
              (selected,metadata.secret_ref or None,metadata.credential_fingerprint or None,_now(),host_id,*self.scope.sql_parameters()))
            self._audit(db,host_id,"credential_rotate",operator_id,"success",
                        detail={"credential_fingerprint":metadata.credential_fingerprint})
            return self._serialize(db,self._host(db,host_id))

    def revoke_credentials(self, host_id: str, *, operator_id: str = "local_operator") -> dict[str, Any]:
        with self.connect() as db:
            self._host(db,host_id)
            self.vault.delete(host_id)
            db.execute("""UPDATE formal_agent_skill_hosts SET secret_ref=NULL,credential_fingerprint=NULL,
              connection_status='pending_test',enabled=0,updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?""",
              (_now(),host_id,*self.scope.sql_parameters()))
            self._audit(db,host_id,"credential_revoke",operator_id,"success")
            return self._serialize(db,self._host(db,host_id))

    def _headers(self, host: sqlite3.Row) -> dict[str, str]:
        auth_type = host["auth_type"]
        headers = {"Accept":"application/json","User-Agent":"IntelligentChannelScheduler/1"}
        if auth_type == "none": return headers
        resolved_type, values = self.vault.load(host["host_id"],host["secret_ref"])
        if resolved_type != auth_type: raise AgentSkillHostError("host_authentication_failed")
        if auth_type == "bearer": headers["Authorization"] = "Bearer " + values["secret"]
        elif auth_type == "api_key_header":
            name = values.get("header_name","X-API-Key")
            if name.casefold() in {"host","content-length","connection","cookie","proxy-authorization"}:
                raise AgentSkillHostError("host_authentication_failed")
            headers[name] = values["secret"]
        else:
            # Token acquisition is intentionally delegated to a transport that
            # supports OAuth without exposing the client secret to callers.
            headers["X-Host-OAuth-Credential"] = "vault-managed"
        return headers

    def _request(self, host: sqlite3.Row, method: str, suffix: str, body: Any | None) -> HostTransportResponse:
        if not self.transport: raise AgentSkillHostError("host_connection_failed")
        base = self._validate_url(host["base_url"])
        url = base.rstrip("/") + "/" + suffix.lstrip("/")
        started = time.monotonic()
        try:
            response = self.transport.request(method=method,url=url,headers=self._headers(host),json_body=body,
                                              timeout_seconds=self.request_timeout_seconds,follow_redirects=False)
        except AgentSkillHostError: raise
        except TimeoutError as exc: raise AgentSkillHostError("invocation_timeout") from exc
        except Exception as exc: raise AgentSkillHostError("host_connection_failed") from exc
        if response.status_code in {301,302,303,307,308}:
            raise AgentSkillHostError("host_connection_failed")
        if response.status_code == 401: raise AgentSkillHostError("host_authentication_failed")
        if response.status_code >= 500: raise AgentSkillHostError("host_connection_failed")
        if response.status_code >= 400:
            remote_code = None
            if isinstance(response.payload,dict):
                remote_code = response.payload.get("code") or response.payload.get("detail")
                if isinstance(remote_code,dict): remote_code = remote_code.get("code")
            if remote_code == "invalid_invocation_schema": remote_code = "schema_validation_failed"
            if remote_code in {"skill_not_installed","skill_not_enabled","schema_validation_failed",
                               "permission_denied","host_not_enabled","invocation_cancelled"}:
                raise AgentSkillHostError(str(remote_code))
            raise AgentSkillHostError("host_protocol_incompatible")
        if not isinstance(response.payload,dict): raise AgentSkillHostError("host_protocol_incompatible")
        if response.latency_ms is None:
            object.__setattr__(response,"latency_ms",int((time.monotonic()-started)*1000))
        return response

    def test_connection(self, host_id: str, *, operator_id: str = "local_operator") -> dict[str, Any]:
        audit_id = f"FSHA-{uuid.uuid4()}"
        with self.connect() as db:
            host = self._host(db,host_id)
            if not str(host["base_url"] or "").strip():
                self._audit(db,host_id,"test_connection",operator_id,"failed",
                            "host_not_configured",{"status":"not_configured"},audit_id)
                db.commit()
                raise AgentSkillHostError("host_not_configured")
            db.execute("UPDATE formal_agent_skill_hosts SET connection_status='testing',updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?",
                       (_now(),host_id,*self.scope.sql_parameters()))
        try:
            response = self._request(host,"GET",".well-known/formal-agent-skill-host",None)
            payload = response.payload
            if payload.get("protocol") not in {None,"formal-agent-host"}:
                raise AgentSkillHostError("host_protocol_incompatible")
            if str(payload.get("protocol_version")) != str(host["protocol_version"]):
                raise AgentSkillHostError("host_protocol_incompatible")
            capabilities = payload.get("capabilities",[]); installed = payload.get("installed_skills",[])
            if not isinstance(capabilities,list) or not isinstance(installed,list):
                raise AgentSkillHostError("host_protocol_incompatible")
            result = {"status":"connected","http_status":response.status_code,"latency_ms":response.latency_ms,
                      "tls":"verified" if response.tls_verified else "failed","authentication":"success",
                      "protocol_version":payload["protocol_version"],"host_version":payload.get("host_version"),
                      "capability_count":len(capabilities),"installed_skill_count":len(installed),"tested_at":_now()}
            with self.connect() as db:
                db.execute("""UPDATE formal_agent_skill_hosts SET connection_status='connected',capabilities_json=?,
                  last_test_result_json=?,last_tested_at=?,updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?""",
                  (_json(redact(capabilities)),_json(result),result["tested_at"],result["tested_at"],host_id,*self.scope.sql_parameters()))
                self._audit(db,host_id,"test_connection",operator_id,"success",detail=result,audit_id=audit_id)
            return {**result,"audit_id":audit_id}
        except Exception as exc:
            code = str(exc) if isinstance(exc,AgentSkillHostError) else "host_connection_failed"
            state = {"host_authentication_failed":"authentication_failed","host_protocol_incompatible":"protocol_incompatible"}.get(code,"network_failed")
            result = {"status":state,"error_code":code,"tested_at":_now()}
            with self.connect() as db:
                db.execute("""UPDATE formal_agent_skill_hosts SET connection_status=?,enabled=0,last_test_result_json=?,
                  last_tested_at=?,updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?""",
                  (state,_json(result),result["tested_at"],result["tested_at"],host_id,*self.scope.sql_parameters()))
                self._audit(db,host_id,"test_connection",operator_id,"failed",code,result,audit_id)
            raise AgentSkillHostError(code) from exc

    def enable_host(self, host_id: str, *, operator_id: str = "local_operator") -> dict[str,Any]:
        with self.connect() as db:
            host=self._host(db,host_id)
            tested = json.loads(host["last_test_result_json"] or "null")
            reconnectable = host["connection_status"] == "disabled" and isinstance(tested,dict) \
                and tested.get("status") == "connected"
            if host["connection_status"]!="connected" and not reconnectable:
                raise AgentSkillHostError("host_not_connected")
            db.execute("UPDATE formal_agent_skill_hosts SET enabled=1,connection_status='connected',updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?",
                       (_now(),host_id,*self.scope.sql_parameters()))
            self._audit(db,host_id,"enable",operator_id,"success")
            return self._serialize(db,self._host(db,host_id))

    def disable_host(self, host_id: str, *, operator_id: str = "local_operator") -> dict[str,Any]:
        with self.connect() as db:
            self._host(db,host_id)
            db.execute("UPDATE formal_agent_skill_hosts SET enabled=0,connection_status='disabled',updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?",
                       (_now(),host_id,*self.scope.sql_parameters()))
            self._audit(db,host_id,"disable",operator_id,"success")
            return self._serialize(db,self._host(db,host_id))

    def capabilities(self,host_id:str)->list[Any]: return self.get_host(host_id)["capabilities"]

    def list_installations(self,host_id:str)->list[dict[str,Any]]:
        with self.connect() as db:
            self._host(db,host_id)
            return [dict(r) for r in db.execute("SELECT * FROM formal_agent_skill_host_installations WHERE host_id=? AND tenant_id=? AND workspace_id=? ORDER BY installed_at DESC",(host_id,*self.scope.sql_parameters()))]

    def install_skill(self,host_id:str,skill_id:str,skill_version:str,*,operator_id:str="local_operator")->dict[str,Any]:
        now, iid=_now(),f"FSHI-{uuid.uuid4()}"
        with self.connect() as db:
            host=self._host(db,host_id)
            if not host["enabled"] or host["connection_status"]!="connected": raise AgentSkillHostError("host_not_enabled")
            if host["host_type"] == "independent_http_runtime":
                response = self._request(host,"POST","v1/skills/install",{
                    "installation_id":iid,"skill_id":skill_id,"skill_version":skill_version,
                    "operator_id":operator_id,"requested_permissions":["read","local_compute"]})
                remote_id = str(response.payload.get("installation_id") or "")
                if not remote_id:
                    raise AgentSkillHostError("host_protocol_incompatible")
                iid = remote_id
            db.execute("""INSERT INTO formal_agent_skill_host_installations VALUES(?,?,?,?,?,?,?,?,?)
              ON CONFLICT(host_id,skill_id,tenant_id,workspace_id) DO UPDATE SET skill_version=excluded.skill_version,status='installed',updated_at=excluded.updated_at""",
              (iid,host_id,skill_id,skill_version,"installed",now,now,*self.scope.sql_parameters()))
            self._audit(db,host_id,"install_skill",operator_id,"success",detail={"skill_id":skill_id,"version":skill_version})
            row = db.execute("""SELECT * FROM formal_agent_skill_host_installations
              WHERE host_id=? AND skill_id=? AND tenant_id=? AND workspace_id=?""",
              (host_id,skill_id,*self.scope.sql_parameters())).fetchone()
            assert row is not None
            return dict(row)

    def uninstall_skill(self,host_id:str,installation_id:str,*,operator_id:str="local_operator")->bool:
        with self.connect() as db:
            host=self._host(db,host_id)
            if host["host_type"] == "independent_http_runtime":
                row=db.execute("""SELECT skill_id FROM formal_agent_skill_host_installations
                  WHERE installation_id=? AND host_id=? AND tenant_id=? AND workspace_id=?""",
                  (installation_id,host_id,*self.scope.sql_parameters())).fetchone()
                if not row: raise AgentSkillHostError("installation_not_found")
                self._request(host,"DELETE",f"v1/skills/{quote(row['skill_id'],safe='')}/installations/{quote(installation_id,safe='')}",None)
            changed=db.execute("DELETE FROM formal_agent_skill_host_installations WHERE installation_id=? AND host_id=? AND tenant_id=? AND workspace_id=?",
                               (installation_id,host_id,*self.scope.sql_parameters())).rowcount
            if not changed: raise AgentSkillHostError("installation_not_found")
            self._audit(db,host_id,"uninstall_skill",operator_id,"success",detail={"installation_id":installation_id})
            return True

    def set_installation_enabled(self,host_id:str,installation_id:str,enabled:bool,*,
                                 operator_id:str="local_operator")->dict[str,Any]:
        with self.connect() as db:
            host=self._host(db,host_id)
            row=db.execute("""SELECT * FROM formal_agent_skill_host_installations
              WHERE installation_id=? AND host_id=? AND tenant_id=? AND workspace_id=?""",
              (installation_id,host_id,*self.scope.sql_parameters())).fetchone()
            if not row: raise AgentSkillHostError("installation_not_found")
            if host["host_type"] == "independent_http_runtime":
                self._request(host,"POST",f"v1/skills/{quote(row['skill_id'],safe='')}/"
                              + ("enable" if enabled else "disable"),None)
            status="installed" if enabled else "disabled"
            db.execute("""UPDATE formal_agent_skill_host_installations SET status=?,updated_at=?
              WHERE installation_id=? AND host_id=? AND tenant_id=? AND workspace_id=?""",
              (status,_now(),installation_id,host_id,*self.scope.sql_parameters()))
            self._audit(db,host_id,"enable_skill" if enabled else "disable_skill",operator_id,
                        "success",detail={"installation_id":installation_id})
            return dict(db.execute("SELECT * FROM formal_agent_skill_host_installations WHERE installation_id=?",
                                   (installation_id,)).fetchone())

    def invoke_external(self,host_id:str,skill_id:str,skill_version:str,arguments:dict[str,Any],*,
                        operator_id:str="local_operator",permission_decision:str="allowed",
                        invocation_id:str|None=None,audit_id:str|None=None)->dict[str,Any]:
        invocation_id=invocation_id or f"FSI-{uuid.uuid4()}"
        audit_id=audit_id or f"FSHA-{uuid.uuid4()}"
        started=_now(); tick=time.monotonic()
        network_called = False
        try:
            if permission_decision!="allowed": raise AgentSkillHostError("permission_denied")
            with self.connect() as db:
                host=self._host(db,host_id)
                if not host["enabled"] or host["connection_status"]!="connected": raise AgentSkillHostError("host_not_enabled")
                installed=db.execute("""SELECT status FROM formal_agent_skill_host_installations WHERE host_id=? AND skill_id=?
                  AND skill_version=? AND tenant_id=? AND workspace_id=?""",
                  (host_id,skill_id,skill_version,*self.scope.sql_parameters())).fetchone()
                if not installed: raise AgentSkillHostError("skill_not_installed")
                if installed["status"] == "disabled": raise AgentSkillHostError("skill_not_enabled")
            network_called = True
            response=self._request(host,"POST",f"v1/skills/{quote(skill_id,safe='')}/invoke",
                                   {"skill_version":skill_version,"arguments":arguments,"invocation_id":invocation_id})
            required={"status","remote_invocation_id","remote_audit_id","installation_id","skill_id",
                      "skill_version","latency_ms","data","network_called","write_occurred"}
            if not required.issubset(response.payload): raise AgentSkillHostError("invalid_host_response")
            safe=redact(response.payload); result="success"; code=None
        except Exception as exc:
            code=str(exc) if isinstance(exc,AgentSkillHostError) else "invocation_failed"
            safe={"status":"failed","error_code":code}; result="failed"
        finished=_now(); latency=int((time.monotonic()-tick)*1000)
        request_id=safe.get("request_id") if isinstance(safe,dict) else None
        decision_id=safe.get("decision_id") if isinstance(safe,dict) else None
        with self.connect() as db:
            db.execute("""INSERT INTO formal_agent_skill_host_invocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (invocation_id,audit_id,host_id,skill_id,skill_version,operator_id,permission_decision,started,finished,
               latency,result,int(network_called),int(bool(safe.get("write_occurred",False))) if isinstance(safe,dict) else 0,
               request_id,decision_id,_json(safe),*self.scope.sql_parameters()))
            db.execute("UPDATE formal_agent_skill_hosts SET last_invoked_at=?,updated_at=? WHERE host_id=? AND tenant_id=? AND workspace_id=?",
                       (finished,finished,host_id,*self.scope.sql_parameters()))
            self._audit(db,host_id,"invoke",operator_id,result,code,{"invocation_id":invocation_id,"skill_id":skill_id},audit_id)
        output={"invocation_id":invocation_id,"audit_id":audit_id,"skill_id":skill_id,"skill_version":skill_version,
                "host_id":host_id,"protocol_version":host["protocol_version"] if 'host' in locals() else None,
                "started_at":started,"finished_at":finished,"latency_ms":latency,"result":safe,
                "network_called":network_called,"write_occurred":bool(safe.get("write_occurred",False)) if isinstance(safe,dict) else False,
                "request_id":request_id,"decision_id":decision_id,"status":result}
        if isinstance(safe,dict):
            output.update({key:safe.get(key) for key in (
                "remote_invocation_id","remote_audit_id","installation_id","execution_mode","data_source")})
        if result=="failed":
            raise AgentSkillHostError(code or "invocation_failed",
                                      invocation_id=invocation_id,audit_id=audit_id,
                                      host_id=host_id,skill_id=skill_id)
        return output

    def invocations(self,host_id:str)->list[dict[str,Any]]:
        with self.connect() as db:
            self._host(db,host_id)
            rows=db.execute("SELECT * FROM formal_agent_skill_host_invocations WHERE host_id=? AND tenant_id=? AND workspace_id=? ORDER BY started_at DESC",
                            (host_id,*self.scope.sql_parameters())).fetchall()
            return [{**dict(r),"safe_result":json.loads(r["safe_result_json"])} for r in rows]

    def audits(self,host_id:str|None=None)->list[dict[str,Any]]:
        with self.connect() as db:
            sql="SELECT * FROM formal_agent_skill_host_audit WHERE tenant_id=? AND workspace_id=?"; params:list[Any]=list(self.scope.sql_parameters())
            if host_id: sql+=" AND host_id=?"; params.append(host_id)
            return [{**dict(r),"safe_detail":json.loads(r["safe_detail_json"])} for r in db.execute(sql+" ORDER BY created_at DESC",params)]
