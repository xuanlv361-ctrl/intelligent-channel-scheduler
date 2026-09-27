"""Independent Formal Agent Host used for real loopback HTTP integration.

This service deliberately has its own process and SQLite database.  It never
receives production credentials and only executes read/local-compute skills.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse


HOST_PROTOCOL = "formal-agent-host"
PROTOCOL_VERSION = "1.0"
HOST_VERSION = "1.1.0"
ALLOWED_PERMISSIONS = frozenset({"read", "local_compute"})
FORBIDDEN_ARGUMENT_KEYS = frozenset({
    "external_network", "network_url", "write_path", "secret", "api_key",
    "authorization", "production_change", "controlled_write",
})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


DB_PATH = Path(os.getenv("EXTERNAL_AGENT_HOST_DB", Path(__file__).parent / "data" / "host.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
BOOTSTRAP_TOKEN = os.getenv("EXTERNAL_AGENT_HOST_TOKEN", "")
ACCEPTANCE_MODE = os.getenv("EXTERNAL_AGENT_HOST_ACCEPTANCE_MODE", "false").lower() == "true"
if not BOOTSTRAP_TOKEN:
    raise RuntimeError("EXTERNAL_AGENT_HOST_TOKEN is required")

app = FastAPI(title="Independent Formal Agent Host", version=HOST_VERSION)


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _init_schema() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS host_settings(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), host_id TEXT NOT NULL,
          host_name TEXT NOT NULL, enabled INTEGER NOT NULL, protocol_version TEXT NOT NULL,
          started_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_credentials(
          credential_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL UNIQUE,
          auth_type TEXT NOT NULL, header_name TEXT, fingerprint TEXT NOT NULL,
          status TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);
        CREATE TABLE IF NOT EXISTS installations(
          installation_id TEXT PRIMARY KEY, skill_id TEXT NOT NULL UNIQUE,
          skill_version TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          requested_permissions_json TEXT NOT NULL DEFAULT '[]',
          installed_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS invocations(
          remote_invocation_id TEXT PRIMARY KEY, remote_audit_id TEXT NOT NULL,
          caller_invocation_id TEXT UNIQUE, idempotency_key TEXT UNIQUE,
          installation_id TEXT, skill_id TEXT, skill_version TEXT,
          status TEXT NOT NULL, latency_ms INTEGER NOT NULL,
          safe_input_json TEXT NOT NULL, safe_result_json TEXT NOT NULL,
          created_at TEXT NOT NULL, finished_at TEXT);
        CREATE TABLE IF NOT EXISTS audit(
          remote_audit_id TEXT PRIMARY KEY, operation TEXT NOT NULL,
          result TEXT NOT NULL, error_code TEXT, safe_detail_json TEXT NOT NULL,
          created_at TEXT NOT NULL);
        """)
        columns = {row[1] for row in db.execute("PRAGMA table_info(invocations)")}
        for name, declaration in {
            "idempotency_key": "TEXT", "finished_at": "TEXT",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE invocations ADD COLUMN {name} {declaration}")
        install_columns = {row[1] for row in db.execute("PRAGMA table_info(installations)")}
        if "requested_permissions_json" not in install_columns:
            db.execute("ALTER TABLE installations ADD COLUMN requested_permissions_json TEXT NOT NULL DEFAULT '[]'")
        stamp = now()
        db.execute("""INSERT OR IGNORE INTO host_settings VALUES(1,?,?,?,?,?,?)""", (
            "EAH-" + str(uuid.uuid4()), "本地独立 Agent Host", 1,
            PROTOCOL_VERSION, stamp, stamp,
        ))
        if not db.execute("SELECT 1 FROM auth_credentials WHERE status='active'").fetchone():
            db.execute("INSERT INTO auth_credentials VALUES(?,?,?,?,?,?,?,NULL)", (
                "EAC-" + str(uuid.uuid4()), digest(BOOTSTRAP_TOKEN), "bearer", None,
                "sha256:" + digest(BOOTSTRAP_TOKEN)[-12:].upper(), "active", stamp,
            ))


_init_schema()


def safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): "<redacted>" if str(k).lower().replace("-", "_") in {
            "authorization", "api_key", "token", "secret", "cookie", "new_credential",
        } else safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(item) for item in value]
    return value


def audit(operation: str, result: str, detail: dict[str, Any], error_code: str | None = None,
          audit_id: str | None = None) -> str:
    audit_id = audit_id or f"RAA-{uuid.uuid4()}"
    with connect() as db:
        db.execute("INSERT INTO audit VALUES(?,?,?,?,?,?)", (
            audit_id, operation, result, error_code,
            json.dumps(safe(detail), ensure_ascii=False, sort_keys=True), now(),
        ))
    return audit_id


def structured_error(status: int, code: str, message: str, **detail: Any) -> JSONResponse:
    audit_id = audit("request_failed", "failed", detail, code)
    return JSONResponse({"code": code, "message": message, "remote_audit_id": audit_id}, status_code=status)


def _credential_from(request: Request) -> tuple[str, str, str | None]:
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        return "bearer", authorization[7:], None
    api_key = request.headers.get("x-api-key", "")
    return "api_key_header", api_key, "X-API-Key"


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.url.path in {"/health", "/ready"}:
        return await call_next(request)
    auth_type, credential, header_name = _credential_from(request)
    matched = False
    if credential:
        with connect() as db:
            row = db.execute("""SELECT 1 FROM auth_credentials
              WHERE secret_hash=? AND auth_type=? AND status='active'
              AND (header_name IS NULL OR header_name=?)""",
              (digest(credential), auth_type, header_name)).fetchone()
            matched = row is not None
    if not matched:
        return structured_error(401, "host_authentication_failed", "宿主鉴权失败。",
                                path=request.url.path, auth_type=auth_type)
    return await call_next(request)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": str(exc.detail)}
    code = str(detail.get("code") or "host_request_failed")
    message = str(detail.get("message") or code)
    audit_id = audit("request_failed", "failed", {"path": request.url.path}, code)
    return JSONResponse({"code": code, "message": message, "remote_audit_id": audit_id},
                        status_code=exc.status_code)


def host_info() -> dict[str, Any]:
    with connect() as db:
        settings = dict(db.execute("SELECT * FROM host_settings WHERE singleton=1").fetchone())
        installed = db.execute("SELECT COUNT(*) FROM installations").fetchone()[0]
        enabled = db.execute("SELECT COUNT(*) FROM installations WHERE enabled=1").fetchone()[0]
    return {
        "host_id": settings["host_id"], "host_name": settings["host_name"],
        "host_version": HOST_VERSION, "protocol": HOST_PROTOCOL,
        "protocol_version": settings["protocol_version"],
        "supported_auth": ["bearer", "api_key_header"],
        "supported_permissions": sorted(ALLOWED_PERMISSIONS),
        "supported_execution_modes": ["instruction_only", "local_compute"],
        "installed_skill_count": installed, "enabled_skill_count": enabled,
        "started_at": settings["started_at"],
        "status": "enabled" if settings["enabled"] else "disabled",
    }


def ensure_host_enabled() -> None:
    with connect() as db:
        enabled = db.execute("SELECT enabled FROM host_settings WHERE singleton=1").fetchone()[0]
    if not enabled:
        raise HTTPException(409, {"code": "host_not_enabled", "message": "宿主当前已停用。"})


@app.get("/health")
def health():
    return {"status": "healthy", "service": "independent-formal-agent-host"}


@app.get("/ready")
def ready():
    with connect() as db:
        db.execute("SELECT 1").fetchone()
    return {"status": "ready", "database": "ready", "protocol_version": PROTOCOL_VERSION}


@app.get("/v1/host/info")
def get_host_info():
    return host_info()


@app.get("/v1/host/capabilities")
def capabilities():
    return {"items": ["skills.install", "skills.invoke", "skills.enable", "skills.disable",
                       "skills.upgrade", "skills.rollback", "skills.uninstall", "invocations.cancel"],
            "permissions": sorted(ALLOWED_PERMISSIONS)}


@app.get("/.well-known/formal-agent-skill-host")
def descriptor():
    with connect() as db:
        installed = [dict(row) for row in db.execute(
            "SELECT installation_id,skill_id,skill_version,enabled FROM installations ORDER BY skill_id")]
    info = host_info()
    return {**info, "capabilities": capabilities()["items"], "installed_skills": installed}


@app.post("/v1/host/enable")
def enable_host():
    with connect() as db:
        db.execute("UPDATE host_settings SET enabled=1,updated_at=? WHERE singleton=1", (now(),))
    return {"status": "enabled", "remote_audit_id": audit("host_enable", "success", {})}


@app.post("/v1/host/disable")
def disable_host():
    with connect() as db:
        db.execute("UPDATE host_settings SET enabled=0,updated_at=? WHERE singleton=1", (now(),))
    return {"status": "disabled", "remote_audit_id": audit("host_disable", "success", {})}


@app.post("/v1/host/credentials/rotate")
def rotate_credential(body: dict[str, Any]):
    new_value = str(body.get("new_credential") or "")
    auth_type = str(body.get("auth_type") or "bearer")
    header_name = "X-API-Key" if auth_type == "api_key_header" else None
    if auth_type not in {"bearer", "api_key_header"} or len(new_value) < 24:
        raise HTTPException(422, {"code": "schema_validation_failed", "message": "新凭据格式无效。"})
    stamp = now()
    with connect() as db:
        db.execute("UPDATE auth_credentials SET status='revoked',revoked_at=? WHERE status='active'", (stamp,))
        db.execute("INSERT INTO auth_credentials VALUES(?,?,?,?,?,?,?,NULL)", (
            "EAC-" + str(uuid.uuid4()), digest(new_value), auth_type, header_name,
            "sha256:" + digest(new_value)[-12:].upper(), "active", stamp,
        ))
    return {"status": "rotated", "credential_fingerprint": "sha256:" + digest(new_value)[-12:].upper(),
            "remote_audit_id": audit("credential_rotate", "success", {"auth_type": auth_type})}


def _permissions(body: dict[str, Any]) -> list[str]:
    values = body.get("requested_permissions", ["read", "local_compute"])
    if not isinstance(values, list) or any(str(item) not in ALLOWED_PERMISSIONS for item in values):
        raise HTTPException(403, {"code": "permission_denied", "message": "宿主仅允许读取和本地计算。"})
    return sorted({str(item) for item in values})


def install_skill(body: dict[str, Any]):
    ensure_host_enabled()
    skill_id, version = str(body.get("skill_id") or ""), str(body.get("skill_version") or body.get("version") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,127}", skill_id) or not version:
        raise HTTPException(422, {"code": "schema_validation_failed", "message": "Skill安装参数无效。"})
    permissions = _permissions(body)
    installation_id = str(body.get("installation_id") or f"RHI-{uuid.uuid4()}")
    timestamp = now()
    with connect() as db:
        existing = db.execute("SELECT installation_id FROM installations WHERE skill_id=?", (skill_id,)).fetchone()
        if existing:
            installation_id = existing["installation_id"]
            db.execute("""UPDATE installations SET skill_version=?,enabled=1,
              requested_permissions_json=?,updated_at=? WHERE skill_id=?""",
              (version, json.dumps(permissions), timestamp, skill_id))
        else:
            db.execute("INSERT INTO installations VALUES(?,?,?,?,?,?,?)", (
                installation_id, skill_id, version, 1, json.dumps(permissions), timestamp, timestamp))
    remote_audit_id = audit("install_or_upgrade", "success", {
        "installation_id": installation_id, "skill_id": skill_id,
        "skill_version": version, "requested_permissions": permissions})
    return {"status": "installed", "installation_id": installation_id,
            "skill_id": skill_id, "skill_version": version, "remote_audit_id": remote_audit_id}


@app.post("/v1/skills/install")
def install_v1(body: dict[str, Any]):
    return install_skill(body)


@app.post("/api/v1/installations")
def install_compat(body: dict[str, Any]):
    return install_skill(body)


def _installation(skill_id: str) -> sqlite3.Row:
    with connect() as db:
        row = db.execute("SELECT * FROM installations WHERE skill_id=?", (skill_id,)).fetchone()
    if not row:
        raise HTTPException(404, {"code": "skill_not_installed", "message": "Skill尚未安装。"})
    return row


def set_skill_enabled(skill_id: str, enabled: bool):
    ensure_host_enabled()
    row = _installation(skill_id)
    with connect() as db:
        db.execute("UPDATE installations SET enabled=?,updated_at=? WHERE skill_id=?", (int(enabled), now(), skill_id))
    operation = "enable" if enabled else "disable"
    return {"status": "enabled" if enabled else "disabled", "installation_id": row["installation_id"],
            "remote_audit_id": audit(operation, "success", {"skill_id": skill_id})}


@app.post("/v1/skills/{skill_id}/enable")
def enable_skill_v1(skill_id: str): return set_skill_enabled(skill_id, True)


@app.post("/v1/skills/{skill_id}/disable")
def disable_skill_v1(skill_id: str): return set_skill_enabled(skill_id, False)


@app.post("/api/v1/installations/{installation_id}/enable")
def enable_installation(installation_id: str):
    with connect() as db:
        row = db.execute("SELECT skill_id FROM installations WHERE installation_id=?", (installation_id,)).fetchone()
    if not row: raise HTTPException(404, {"code": "skill_not_installed"})
    return set_skill_enabled(row["skill_id"], True)


@app.post("/api/v1/installations/{installation_id}/disable")
def disable_installation(installation_id: str):
    with connect() as db:
        row = db.execute("SELECT skill_id FROM installations WHERE installation_id=?", (installation_id,)).fetchone()
    if not row: raise HTTPException(404, {"code": "skill_not_installed"})
    return set_skill_enabled(row["skill_id"], False)


def uninstall(installation_id: str):
    ensure_host_enabled()
    with connect() as db:
        changed = db.execute("DELETE FROM installations WHERE installation_id=?", (installation_id,)).rowcount
    if not changed: raise HTTPException(404, {"code": "skill_not_installed"})
    return {"status": "uninstalled", "remote_audit_id": audit("uninstall", "success", {"installation_id": installation_id})}


@app.delete("/v1/skills/{skill_id}/installations/{installation_id}")
def uninstall_v1(skill_id: str, installation_id: str):
    row = _installation(skill_id)
    if row["installation_id"] != installation_id:
        raise HTTPException(404, {"code": "skill_not_installed"})
    return uninstall(installation_id)


@app.delete("/api/v1/installations/{installation_id}")
def uninstall_compat(installation_id: str): return uninstall(installation_id)


@app.get("/v1/skills")
def list_skills():
    with connect() as db:
        return {"items": [dict(row) for row in db.execute("SELECT * FROM installations ORDER BY skill_id")]}


def execute_frontend_design(arguments: dict[str, Any], version: str) -> dict[str, Any]:
    raw_task = arguments.get("task")
    if not isinstance(raw_task, str) or not raw_task.strip():
        raise HTTPException(422, {"code": "schema_validation_failed", "message": "请输入需要评审的界面任务。"})
    task = raw_task.strip()
    lower = task.casefold()
    checks = {
        "responsive": any(word in lower for word in ("移动", "窄屏", "responsive", "mobile")),
        "accessibility": any(word in lower for word in ("无障碍", "键盘", "aria", "accessibility")),
        "hierarchy": any(word in lower for word in ("层级", "信息架构", "主操作", "hierarchy")),
        "real_data": any(word in lower for word in ("真实", "数据来源", "证据", "real data")),
    }
    recommendations = []
    if not checks["hierarchy"]: recommendations.append("明确页面唯一主任务与操作层级")
    if not checks["responsive"]: recommendations.append("补充桌面与窄屏布局约束")
    if not checks["accessibility"]: recommendations.append("补充键盘焦点、语义标签和状态文字")
    if not checks["real_data"]: recommendations.append("明确真实数据来源和空状态")
    if not recommendations: recommendations.append("约束完整，可进入组件与视觉细化")
    return {"summary": f"已分析 {len(task)} 个字符的界面任务", "skill_version": version,
            "task_sha256": digest(task), "checks": checks, "recommendations": recommendations,
            "data_source": "remote_instruction_analysis", "execution_mode": "local_compute"}


def invoke_skill(skill_id: str, body: dict[str, Any], request: Request):
    ensure_host_enabled()
    started = time.monotonic()
    version, arguments = str(body.get("skill_version") or ""), body.get("arguments")
    if not isinstance(arguments, dict):
        raise HTTPException(422, {"code": "schema_validation_failed", "message": "调用输入必须是对象。"})
    if any(key in arguments for key in FORBIDDEN_ARGUMENT_KEYS):
        raise HTTPException(403, {"code": "permission_denied", "message": "该Host不允许网络、写入或Secret权限。"})
    row = _installation(skill_id)
    if row["skill_version"] != version: raise HTTPException(404, {"code": "skill_not_installed"})
    if not row["enabled"]: raise HTTPException(409, {"code": "skill_not_enabled"})
    caller_id = str(body.get("invocation_id") or "") or None
    idem = request.headers.get("idempotency-key") or caller_id
    if idem:
        with connect() as db:
            prior = db.execute("SELECT safe_result_json FROM invocations WHERE idempotency_key=?", (idem,)).fetchone()
        if prior:
            result = json.loads(prior["safe_result_json"])
            audit("invoke_idempotent_replay", "success", {"idempotency_key_sha256": digest(idem)})
            return {**result, "idempotent_replay": True}
    if ACCEPTANCE_MODE and arguments.get("test_mode") == "invalid_response":
        audit("invoke", "failed", {"skill_id": skill_id}, "invalid_host_response")
        return {"damaged": True}
    delay_ms = min(max(int(arguments.get("delay_ms") or 0), 0), 35000)
    remote_invocation_id, remote_audit_id = f"RIN-{uuid.uuid4()}", f"RAA-{uuid.uuid4()}"
    created = now()
    with connect() as db:
        db.execute("INSERT INTO invocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            remote_invocation_id, remote_audit_id, caller_id, idem, row["installation_id"], skill_id,
            version, "running", 0, json.dumps(safe(arguments), ensure_ascii=False), "{}", created, None))
    remaining = delay_ms
    while remaining > 0:
        time.sleep(min(remaining, 50) / 1000)
        remaining -= 50
        with connect() as db:
            status = db.execute("SELECT status FROM invocations WHERE remote_invocation_id=?", (remote_invocation_id,)).fetchone()[0]
        if status == "cancelled":
            audit("invoke", "failed", {"remote_invocation_id": remote_invocation_id}, "invocation_cancelled", remote_audit_id)
            raise HTTPException(409, {"code": "invocation_cancelled", "message": "调用已取消。"})
    result = execute_frontend_design(arguments, version) if skill_id == "frontend-design" else {
        "summary": "Skill已在独立Host执行", "skill_version": version,
        "input_sha256": digest(json.dumps(arguments, ensure_ascii=False, sort_keys=True)),
        "data_source": "remote_skill_runtime", "execution_mode": "local_compute"}
    latency = int((time.monotonic() - started) * 1000)
    payload = {"status": "success", "remote_invocation_id": remote_invocation_id,
               "remote_audit_id": remote_audit_id, "installation_id": row["installation_id"],
               "skill_id": skill_id, "skill_version": version, "latency_ms": latency,
               "started_at": created, "finished_at": now(), "execution_mode": "local_compute",
               "network_called": False, "write_occurred": False, "data_source": "independent_agent_host",
               "data": result}
    with connect() as db:
        db.execute("""UPDATE invocations SET status='success',latency_ms=?,safe_result_json=?,finished_at=?
          WHERE remote_invocation_id=?""", (latency, json.dumps(payload, ensure_ascii=False), payload["finished_at"], remote_invocation_id))
    audit("invoke", "success", {"remote_invocation_id": remote_invocation_id, "skill_id": skill_id}, audit_id=remote_audit_id)
    return payload


@app.post("/v1/skills/{skill_id}/invoke")
def invoke_v1(skill_id: str, body: dict[str, Any], request: Request): return invoke_skill(skill_id, body, request)


@app.post("/api/v1/skills/{skill_id}/invoke")
def invoke_compat(skill_id: str, body: dict[str, Any], request: Request): return invoke_skill(skill_id, body, request)


@app.delete("/v1/invocations/{remote_invocation_id}")
def cancel_invocation(remote_invocation_id: str):
    with connect() as db:
        changed = db.execute("UPDATE invocations SET status='cancelled',finished_at=? WHERE remote_invocation_id=? AND status='running'",
                             (now(), remote_invocation_id)).rowcount
    if not changed: raise HTTPException(409, {"code": "invocation_not_cancellable"})
    return {"status": "cancelled", "remote_audit_id": audit("cancel", "success", {"remote_invocation_id": remote_invocation_id})}


@app.get("/v1/invocations")
@app.get("/api/v1/invocations")
def invocations():
    with connect() as db:
        return {"items": [dict(row) for row in db.execute("SELECT * FROM invocations ORDER BY created_at DESC")]}


@app.get("/v1/audit-records")
@app.get("/api/v1/audit")
def audits():
    with connect() as db:
        return {"items": [dict(row) for row in db.execute("SELECT * FROM audit ORDER BY created_at DESC")]}
