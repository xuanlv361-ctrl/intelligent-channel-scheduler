"""Persistent lifecycle catalog layered on the existing Formal Skill runtime."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from backend.tenant_security import TenantScope
from formal_agent_skill_registry import FormalAgentSkillError, validate_schema


VERSION_STATES = {"draft", "validated", "published", "deprecated"}
INSTALL_STATES = {"not_installed", "installed", "enabled", "disabled", "uninstalled"}
PERMISSIONS = {"read", "local_compute", "controlled_write", "external_network",
               "secret_use", "production_change"}

SCHEMA_PROPERTIES: dict[str, dict[str, dict[str, Any]]] = {
    "explain-routing-decision": {
        "decision_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "include_metrics": {"type": "boolean", "default": True},
        "include_execution": {"type": "boolean", "default": True},
    },
    "inspect-channel-health": {
        "environment_id": {"type": "string", "enum": ["china_uat"]},
        "channel_id": {"type": "string", "maxLength": 256},
        "time_range": {"type": "string", "maxLength": 128},
    },
    "analyze-model-cost": {
        "environment_id": {"type": "string", "enum": ["china_uat"]},
        "model_id": {"type": "string", "maxLength": 256},
        "time_range": {"type": "string", "maxLength": 128},
        "group_by": {"type": "string", "enum": ["model", "day", "source"]},
    },
    "classify-provider-error": {
        "request_id": {"type": "string", "maxLength": 256},
        "error_id": {"type": "string", "maxLength": 256},
    },
    "collect-acceptance-evidence": {
        "acceptance_suite_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "environment_id": {"type": "string", "enum": ["china_uat"]},
        "time_range": {"type": "string", "maxLength": 128},
    },
    "validate-uat-request": {
        "environment_id": {"type": "string", "enum": ["china_uat"]},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
        "headers": {"type": "array"}, "body": {}, "model_id": {"type": "string"},
        "multimodal_metadata": {"type": "object"},
    },
    "execute-uat-request": {
        "environment_id": {"type": "string", "enum": ["china_uat"]},
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
        "endpoint": {"type": "string", "minLength": 1, "maxLength": 2048},
        "headers": {"type": "array"}, "body": {}, "model_id": {"type": "string"},
    },
    "update-routing-policy": {
        "policy_id": {"type": "string", "minLength": 1},
        "expected_version": {"type": "string", "minLength": 1},
        "changes": {"type": "object"}, "reason": {"type": "string", "minLength": 1},
        "confirmed": {"type": "boolean"},
    },
    "operate-circuit-breaker": {
        "action": {"type": "string", "enum": ["open", "half_open", "run_probe", "recover", "force_close"]},
        "circuit_id": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
        "probe_lease_id": {"type": "string"}, "confirmed": {"type": "boolean"},
    },
    "propose-traffic-switch": {
        "source_policy_version": {"type": "string", "minLength": 1},
        "target_policy_version": {"type": "string", "minLength": 1},
        "requested_percentage": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
        "environment": {"type": "string", "enum": ["china_uat"]},
        "reason": {"type": "string", "minLength": 1},
        "rollback_condition": {"type": "string", "minLength": 1},
        "confirmed": {"type": "boolean"},
    },
}


BUSINESS_SKILLS: tuple[dict[str, Any], ...] = (
    {"skill_id":"explain-routing-decision","display_name":"解释调度决策","binding":"scheduler_attribution.read",
     "permissions":["read","local_compute"],"pages":["routing-runs","shadow-routing","historical-replay"],
     "required":["decision_id"]},
    {"skill_id":"inspect-channel-health","display_name":"检查渠道健康","binding":"channel_health.read",
     "permissions":["read","local_compute"],"pages":["channel-health","monitoring-overview"],
     "required":["environment_id"]},
    {"skill_id":"analyze-model-cost","display_name":"分析模型Token与费用","binding":"model_cost.read",
     "permissions":["read","local_compute"],"pages":["token-cost","model-detail","dashboard"],
     "required":["environment_id"]},
    {"skill_id":"classify-provider-error","display_name":"分类模型与渠道错误","binding":"provider_error.read",
     "permissions":["read","local_compute"],"pages":["errors","execution-detail"],"required":[]},
    {"skill_id":"collect-acceptance-evidence","display_name":"收集验收证据","binding":"acceptance_evidence.read",
     "permissions":["read","local_compute"],"pages":["acceptance"],
     "required":["acceptance_suite_id","environment_id"]},
    {"skill_id":"validate-uat-request","display_name":"验证UAT请求","binding":"uat_request.validate",
     "permissions":["read","local_compute"],"pages":["routing-execute"],
     "required":["environment_id","method","endpoint"]},
    {"skill_id":"execute-uat-request","display_name":"执行真实UAT请求","binding":"uat_request.execute",
     "permissions":["read","external_network","secret_use"],"pages":["routing-execute"],
     "required":["environment_id","method","endpoint"]},
    {"skill_id":"update-routing-policy","display_name":"修改调度策略","binding":"routing_policy.write",
     "permissions":["read","controlled_write"],"pages":["strategy-config","config-review"],
     "required":["policy_id","expected_version","changes","reason"],"confirmation_required":True},
    {"skill_id":"operate-circuit-breaker","display_name":"操作熔断与恢复","binding":"circuit_breaker.write",
     "permissions":["read","controlled_write"],"pages":["circuit-breakers"],
     "required":["action","circuit_id","reason"],"confirmation_required":True},
    {"skill_id":"propose-traffic-switch","display_name":"创建流量切换提案","binding":"traffic_proposal.write",
     "permissions":["read","controlled_write"],"pages":["traffic-governance"],
     "required":["source_policy_version","target_policy_version","requested_percentage","environment","reason","rollback_condition"],
     "confirmation_required":True},
)


class SkillLifecycleError(ValueError):
    """A stable lifecycle failure with safe context for the public API.

    The error code remains the string representation for backwards
    compatibility.  Routes can use the attached fields instead of attempting
    to reconstruct state after the transaction has ended.
    """

    def __init__(self, code: str, *, skill_id: str | None = None,
                 version: str | None = None,
                 lifecycle_status: str | None = None,
                 installation_status: str | None = None,
                 required_action: str | None = None,
                 safe_detail: Mapping[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.skill_id = skill_id
        self.version = version
        self.lifecycle_status = lifecycle_status
        self.installation_status = installation_status
        self.required_action = required_action
        self.safe_detail = dict(safe_detail or {})

    def context(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "lifecycle_status": self.lifecycle_status,
            "installation_status": self.installation_status,
            "required_action": self.required_action,
            "safe_detail": dict(self.safe_detail),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    raw = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(raw).hexdigest().upper()


class FormalAgentSkillLifecycleService:
    def __init__(self, database_path: str | Path, scope: TenantScope | None = None):
        self.path = Path(database_path)
        self.scope = scope or TenantScope.local_development()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self.seed_business_skills()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS formal_agent_skill_sources(
              skill_id TEXT NOT NULL,source_type TEXT NOT NULL,source_uri TEXT,
              repository_commit TEXT,imported_at TEXT NOT NULL,content_sha256 TEXT NOT NULL,
              license_name TEXT,content_text TEXT NOT NULL,toml_source_json TEXT,
              scripts_json TEXT NOT NULL,dependencies_json TEXT NOT NULL,references_json TEXT NOT NULL,
              requested_domains_json TEXT NOT NULL,security_result_json TEXT NOT NULL,
              imported_by TEXT NOT NULL,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
              PRIMARY KEY(skill_id,tenant_id,workspace_id));
            CREATE TABLE IF NOT EXISTS formal_agent_skill_versions(
              skill_id TEXT NOT NULL,version TEXT NOT NULL,display_name TEXT NOT NULL,
              description TEXT NOT NULL,source_type TEXT NOT NULL,content_sha256 TEXT NOT NULL,
              version_status TEXT NOT NULL,input_schema_json TEXT NOT NULL,output_schema_json TEXT NOT NULL,
              requested_permissions_json TEXT NOT NULL,allowed_environments_json TEXT NOT NULL,
              allowed_resources_json TEXT NOT NULL,allowed_domains_json TEXT NOT NULL,
              confirmation_required INTEGER NOT NULL,approval_required INTEGER NOT NULL,
              timeout_seconds INTEGER NOT NULL,max_output_size INTEGER NOT NULL,binding TEXT NOT NULL,
              page_ids_json TEXT NOT NULL,trust_status TEXT NOT NULL,created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,previous_version TEXT,change_summary TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL,
              PRIMARY KEY(skill_id,version,tenant_id,workspace_id));
            CREATE TABLE IF NOT EXISTS formal_agent_skill_installations(
              installation_id TEXT PRIMARY KEY,skill_id TEXT NOT NULL,installed_version TEXT,
              previous_version TEXT,installation_status TEXT NOT NULL,installed_at TEXT,
              updated_at TEXT NOT NULL,operator_id TEXT NOT NULL,tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,UNIQUE(skill_id,tenant_id,workspace_id));
            CREATE TABLE IF NOT EXISTS formal_agent_skill_lifecycle_audit(
              event_id TEXT PRIMARY KEY,skill_id TEXT NOT NULL,version TEXT,operation TEXT NOT NULL,
              from_state TEXT,to_state TEXT,result TEXT NOT NULL,operator_id TEXT NOT NULL,
              reason TEXT,created_at TEXT NOT NULL,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS formal_agent_skill_invocations(
              invocation_id TEXT PRIMARY KEY,audit_id TEXT NOT NULL,operator_id TEXT NOT NULL,
              skill_id TEXT NOT NULL,skill_version TEXT NOT NULL,started_at TEXT NOT NULL,
              finished_at TEXT NOT NULL,permission_decision TEXT NOT NULL,execution_result TEXT NOT NULL,
              request_id TEXT,response_id TEXT,decision_id TEXT,network_called INTEGER NOT NULL,
              write_performed INTEGER NOT NULL,input_sha256 TEXT NOT NULL,safe_result_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS formal_agent_skill_hosts(
              host_id TEXT PRIMARY KEY,host_name TEXT NOT NULL,host_type TEXT NOT NULL,base_url TEXT,
              enabled INTEGER NOT NULL DEFAULT 0,connection_status TEXT NOT NULL DEFAULT 'not_tested',
              last_tested_at TEXT,tenant_id TEXT NOT NULL,workspace_id TEXT NOT NULL);
            """)
            columns={row[1] for row in db.execute("PRAGMA table_info(formal_agent_skill_invocations)")}
            for name,kind in {
                "execution_engine":"TEXT", "model_id":"TEXT", "host_id":"TEXT",
                "input_summary":"TEXT", "result_summary":"TEXT", "latency_ms":"INTEGER",
                "input_tokens":"INTEGER", "output_tokens":"INTEGER"
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE formal_agent_skill_invocations ADD COLUMN {name} {kind}")

    def _audit(self, db: sqlite3.Connection, skill_id: str, version: str | None,
               operation: str, from_state: str | None, to_state: str | None,
               operator_id: str, result: str = "success", reason: str | None = None) -> None:
        db.execute("""INSERT INTO formal_agent_skill_lifecycle_audit VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (f"FSL-{uuid.uuid4()}", skill_id, version, operation, from_state, to_state,
                    result, operator_id, reason, _now(), *self.scope.sql_parameters()))

    @staticmethod
    def _schema(skill_id: str, required: list[str]) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": True, "required": required,
                "properties": SCHEMA_PROPERTIES.get(skill_id, {
                    key: {"type": "string", "maxLength": 4096} for key in required})}

    def seed_business_skills(self) -> None:
        with self.connect() as db:
            for item in BUSINESS_SKILLS:
                skill_id = item["skill_id"]
                existing = db.execute("""SELECT 1 FROM formal_agent_skill_versions WHERE skill_id=?
                  AND version='1.0.0' AND tenant_id=? AND workspace_id=?""",
                  (skill_id, *self.scope.sql_parameters())).fetchone()
                if existing:
                    db.execute("""UPDATE formal_agent_skill_versions SET display_name=?,description=?,
                      input_schema_json=?,requested_permissions_json=?,allowed_environments_json=?,
                      allowed_resources_json=?,allowed_domains_json=?,confirmation_required=?,binding=?,
                      page_ids_json=? WHERE skill_id=? AND version='1.0.0' AND tenant_id=? AND workspace_id=?""",(
                        item["display_name"], item["display_name"],
                        _canonical(self._schema(skill_id, item["required"])), _canonical(item["permissions"]),
                        _canonical(["china_uat"]), _canonical(item["pages"]),
                        _canonical(["api-uat.weimeta.cn"] if skill_id == "execute-uat-request" else []),
                        int(item.get("confirmation_required", False)), item["binding"], _canonical(item["pages"]),
                        skill_id, *self.scope.sql_parameters()))
                    continue
                content = _canonical(item)
                db.execute("""INSERT INTO formal_agent_skill_versions(
                  skill_id,version,display_name,description,source_type,content_sha256,
                  version_status,input_schema_json,output_schema_json,requested_permissions_json,
                  allowed_environments_json,allowed_resources_json,allowed_domains_json,
                  confirmation_required,approval_required,timeout_seconds,max_output_size,binding,
                  page_ids_json,trust_status,created_at,created_by,previous_version,change_summary,
                  tenant_id,workspace_id) VALUES(
                  ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    skill_id,"1.0.0",item["display_name"],item["display_name"],"system_business_skill",
                    _sha(content),"published",_canonical(self._schema(skill_id,item["required"])),
                    _canonical({"type":"object"}),_canonical(item["permissions"]),_canonical(["china_uat"]),
                    _canonical(item["pages"]),_canonical(["api-uat.weimeta.cn"] if skill_id=="execute-uat-request" else []),
                    int(item.get("confirmation_required",False)),0,120,1048576,item["binding"],
                    _canonical(item["pages"]),"trusted_internal",_now(),"system-bootstrap",None,
                    "initial system business skill",*self.scope.sql_parameters()))
                installation_id = f"FSI-{uuid.uuid4()}"
                db.execute("""INSERT OR IGNORE INTO formal_agent_skill_installations VALUES(
                  ?,?,'1.0.0',NULL,'enabled',?,?,?, ?,?)""",
                  (installation_id,skill_id,_now(),_now(),"system-bootstrap",*self.scope.sql_parameters()))
                self._audit(db,skill_id,"1.0.0","bootstrap",None,"enabled","system-bootstrap")

    def import_external(self, *, skill_id: str, display_name: str, description: str,
                        source_type: str, source_uri: str, content: str,
                        operator_id: str, repository_commit: str | None = None,
                        license_name: str | None = None, toml_source: Mapping[str, Any] | None = None,
                        scripts: list[str] | None = None, dependencies: list[str] | None = None,
                        references: list[str] | None = None) -> dict[str, Any]:
        if not skill_id or not content.strip():
            raise SkillLifecycleError("skill_import_content_required")
        digest = _sha(content.replace("\r\n","\n"))
        is_toml = source_type == "codex_agent_toml"
        allowed_toml_keys = {"name", "description", "developer_instructions"}
        toml_fields_only = bool(toml_source) and set(toml_source or {}) <= allowed_toml_keys
        toml_instructions = str((toml_source or {}).get("developer_instructions") or "").strip()
        toml_safe = (is_toml and toml_fields_only and bool(toml_instructions)
                     and not scripts and not dependencies and not references)
        safe = not scripts and bool(source_uri) and bool(digest)
        security = {"source_known":bool(source_uri),"integrity_verified":True,
                    "license_known":bool(license_name),"scripts_reviewed":not scripts,
                    "schema_validated":False,
                    "toml_fields_allowlisted":toml_safe if is_toml else None,
                    "publish_allowed":safe and (bool(license_name) or toml_safe)}
        external_input_schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "task": {"type": "string", "minLength": 1, "maxLength": 16000}
            },
        }
        with self.connect() as db:
            db.execute("""INSERT INTO formal_agent_skill_sources VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(skill_id,tenant_id,workspace_id)
              DO UPDATE SET source_type=excluded.source_type,source_uri=excluded.source_uri,
              repository_commit=excluded.repository_commit,imported_at=excluded.imported_at,
              content_sha256=excluded.content_sha256,license_name=excluded.license_name,
              content_text=excluded.content_text,toml_source_json=excluded.toml_source_json,
              scripts_json=excluded.scripts_json,dependencies_json=excluded.dependencies_json,
              references_json=excluded.references_json,security_result_json=excluded.security_result_json,
              imported_by=excluded.imported_by""",(
                skill_id,source_type,source_uri,repository_commit,_now(),digest,license_name,content,
                _canonical(toml_source) if toml_source else None,_canonical(scripts or []),
                _canonical(dependencies or []),_canonical(references or []),_canonical([]),
                _canonical(security),operator_id,*self.scope.sql_parameters()))
            version = "0.1.0"
            db.execute("""INSERT OR REPLACE INTO formal_agent_skill_versions(
              skill_id,version,display_name,description,source_type,content_sha256,
              version_status,input_schema_json,output_schema_json,requested_permissions_json,
              allowed_environments_json,allowed_resources_json,allowed_domains_json,
              confirmation_required,approval_required,timeout_seconds,max_output_size,binding,
              page_ids_json,trust_status,created_at,created_by,previous_version,change_summary,
              tenant_id,workspace_id) VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                skill_id,version,display_name,description,source_type,digest,"draft",
                _canonical(external_input_schema),
                _canonical({"type":"object"}),_canonical(["read","local_compute"]),
                _canonical([]),_canonical([]),_canonical([]),0,0,60,262144,
                "instruction_only",_canonical(["agent-skill-catalog"]),
                "unreviewed" if source_type=="codex_agent_toml" else "imported_unreviewed",
                _now(),operator_id,None,"external import",*self.scope.sql_parameters()))
            db.execute("""INSERT OR IGNORE INTO formal_agent_skill_installations VALUES(
              ?,?,NULL,NULL,'not_installed',NULL,?,?,?,?)""",
              (f"FSI-{uuid.uuid4()}",skill_id,_now(),operator_id,*self.scope.sql_parameters()))
            self._audit(db,skill_id,version,"import",None,"draft",operator_id)
        return self.get(skill_id)

    def _version(self, db: sqlite3.Connection, skill_id: str, version: str | None = None) -> sqlite3.Row:
        if version:
            row = db.execute("""SELECT * FROM formal_agent_skill_versions WHERE skill_id=? AND version=?
              AND tenant_id=? AND workspace_id=?""",(skill_id,version,*self.scope.sql_parameters())).fetchone()
        else:
            row = db.execute("""SELECT * FROM formal_agent_skill_versions WHERE skill_id=?
              AND tenant_id=? AND workspace_id=? ORDER BY rowid DESC LIMIT 1""",
              (skill_id,*self.scope.sql_parameters())).fetchone()
        if not row:
            raise SkillLifecycleError(
                "version_not_found" if version else "skill_not_found",
                skill_id=skill_id, version=version,
                required_action="select_version" if version else "select_skill")
        return row

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item=dict(row)
        for key in ("input_schema_json","output_schema_json","requested_permissions_json",
                    "allowed_environments_json","allowed_resources_json","allowed_domains_json","page_ids_json"):
            item[key.removesuffix("_json")]=json.loads(item.pop(key))
        item["confirmation_required"]=bool(item["confirmation_required"])
        item["approval_required"]=bool(item["approval_required"])
        return item

    def list(self) -> dict[str, Any]:
        with self.connect() as db:
            rows=db.execute("""SELECT v.*,i.installation_id,i.installation_status,i.installed_version,
              i.previous_version AS installed_previous_version,
              (SELECT MAX(started_at) FROM formal_agent_skill_invocations x WHERE x.skill_id=v.skill_id
               AND x.tenant_id=v.tenant_id AND x.workspace_id=v.workspace_id) AS last_invoked_at
              FROM formal_agent_skill_versions v JOIN (
                SELECT skill_id,MAX(rowid) rowid FROM formal_agent_skill_versions
                WHERE tenant_id=? AND workspace_id=? GROUP BY skill_id) latest ON latest.rowid=v.rowid
              LEFT JOIN formal_agent_skill_installations i ON i.skill_id=v.skill_id
               AND i.tenant_id=v.tenant_id AND i.workspace_id=v.workspace_id
              ORDER BY v.source_type,v.skill_id""",self.scope.sql_parameters()).fetchall()
            items=[self._decode(row) for row in rows]
            audits=db.execute("""SELECT COUNT(*) FROM formal_agent_skill_lifecycle_audit WHERE
              tenant_id=? AND workspace_id=?""",self.scope.sql_parameters()).fetchone()[0]
        return {"items":items,"registered_count":len(items),
                "published_count":sum(x["version_status"]=="published" for x in items),
                "installed_count":sum(x.get("installation_status") in {"installed","enabled","disabled"} for x in items),
                "enabled_count":sum(x.get("installation_status")=="enabled" for x in items),
                "pending_count":sum(x["version_status"] in {"draft","validated"} for x in items),
                "audit_count":audits,"last_synced_at":_now()}

    def get(self, skill_id: str) -> dict[str, Any]:
        with self.connect() as db:
            versions=[self._decode(row) for row in db.execute("""SELECT * FROM formal_agent_skill_versions
              WHERE skill_id=? AND tenant_id=? AND workspace_id=? ORDER BY rowid DESC""",
              (skill_id,*self.scope.sql_parameters()))]
            if not versions: raise SkillLifecycleError("skill_not_found")
            installation=db.execute("""SELECT * FROM formal_agent_skill_installations WHERE skill_id=?
              AND tenant_id=? AND workspace_id=?""",(skill_id,*self.scope.sql_parameters())).fetchone()
            source=db.execute("""SELECT * FROM formal_agent_skill_sources WHERE skill_id=?
              AND tenant_id=? AND workspace_id=?""",(skill_id,*self.scope.sql_parameters())).fetchone()
        return {"skill":versions[0],"versions":versions,
                "installation":dict(installation) if installation else None,
                "source":dict(source) if source else None}

    def transition_version(self, skill_id: str, version: str, operation: str,
                           operator_id: str) -> dict[str, Any]:
        expected={"validate":("draft","validated"),"publish":("validated","published"),
                  "deprecate":("published","deprecated")}
        if operation not in expected: raise SkillLifecycleError("skill_version_operation_invalid")
        before,after=expected[operation]
        with self.connect() as db:
            row=self._version(db,skill_id,version)
            if row["version_status"]!=before:
                code = "skill_not_validated" if operation == "publish" and row["version_status"] == "draft" else "skill_version_transition_invalid"
                raise SkillLifecycleError(
                    code, skill_id=skill_id, version=version,
                    lifecycle_status=row["version_status"],
                    required_action="validate" if code == "skill_not_validated" else operation)
            if operation in {"validate","publish"} and row["source_type"]!="system_business_skill":
                source=db.execute("""SELECT security_result_json,content_text,toml_source_json FROM formal_agent_skill_sources WHERE skill_id=?
                  AND tenant_id=? AND workspace_id=?""",(skill_id,*self.scope.sql_parameters())).fetchone()
                security = json.loads(source["security_result_json"]) if source else {}
                # Validation is deliberately local and non-executing.  A TOML
                # adapter is instruction data only; it can never become a
                # script, network, write or secret-capable Skill.
                schema = json.loads(row["input_schema_json"])
                if (not source or not source["content_text"].strip() or schema.get("type") != "object"
                        or not isinstance(schema.get("properties", {}), dict)):
                    raise SkillLifecycleError(
                        "schema_validation_failed", skill_id=skill_id, version=version,
                        lifecycle_status=row["version_status"],
                        required_action="correct_input")
                if row["source_type"] == "codex_agent_toml":
                    toml = json.loads(source["toml_source_json"] or "{}")
                    if (set(toml) - {"name", "description", "developer_instructions"}
                            or not str(toml.get("developer_instructions") or "").strip()
                            or row["binding"] != "instruction_only"
                            or json.loads(row["allowed_domains_json"])
                            or set(json.loads(row["requested_permissions_json"]))
                            - {"read", "local_compute"}):
                        raise SkillLifecycleError(
                            "skill_external_security_review_incomplete", skill_id=skill_id,
                            version=version, lifecycle_status=row["version_status"],
                            required_action="security_review")
                    # Legacy TOML imports predate publish_allowed.  Passing this
                    # deterministic, non-executing allow-list review is the
                    # authoritative security decision for instruction data.
                    security.update({
                        "toml_fields_allowlisted": True,
                        "schema_validated": True,
                        "security_review_completed": True,
                        "review_type": "deterministic_local_toml_adapter_v1",
                        "reviewed_at": _now(),
                        "reviewed_by": operator_id,
                        "execution_mode": "instruction_only",
                        "network_permission": "deny",
                        "write_permission": "deny",
                        "secret_permission": "deny",
                        "publish_allowed": True,
                    })
                elif not security.get("publish_allowed"):
                    raise SkillLifecycleError(
                        "skill_external_security_review_incomplete", skill_id=skill_id,
                        version=version, lifecycle_status=row["version_status"],
                        required_action="security_review")
                security["schema_validated"] = True
                db.execute("""UPDATE formal_agent_skill_sources SET security_result_json=?
                  WHERE skill_id=? AND tenant_id=? AND workspace_id=?""",
                  (_canonical(security), skill_id, *self.scope.sql_parameters()))
            db.execute("""UPDATE formal_agent_skill_versions SET version_status=? WHERE skill_id=? AND version=?
              AND tenant_id=? AND workspace_id=?""",(after,skill_id,version,*self.scope.sql_parameters()))
            self._audit(db,skill_id,version,operation,before,after,operator_id)
        return self.get(skill_id)

    def create_version(self, skill_id: str, *, version: str, content: str,
                       operator_id: str, change_summary: str) -> dict[str, Any]:
        if not version.strip() or not content.strip() or not change_summary.strip():
            raise SkillLifecycleError("skill_version_content_required")
        with self.connect() as db:
            prior = self._version(db, skill_id)
            if db.execute("""SELECT 1 FROM formal_agent_skill_versions WHERE skill_id=?
              AND version=? AND tenant_id=? AND workspace_id=?""",
              (skill_id, version, *self.scope.sql_parameters())).fetchone():
                raise SkillLifecycleError("skill_version_already_exists")
            values = dict(prior)
            db.execute("""INSERT INTO formal_agent_skill_versions(
              skill_id,version,display_name,description,source_type,content_sha256,
              version_status,input_schema_json,output_schema_json,requested_permissions_json,
              allowed_environments_json,allowed_resources_json,allowed_domains_json,
              confirmation_required,approval_required,timeout_seconds,max_output_size,binding,
              page_ids_json,trust_status,created_at,created_by,previous_version,change_summary,
              tenant_id,workspace_id) VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                skill_id, version, values["display_name"], values["description"],
                values["source_type"], _sha(content.replace("\r\n", "\n")), "draft",
                values["input_schema_json"], values["output_schema_json"],
                values["requested_permissions_json"], values["allowed_environments_json"],
                values["allowed_resources_json"], values["allowed_domains_json"],
                values["confirmation_required"], values["approval_required"],
                values["timeout_seconds"], values["max_output_size"], values["binding"],
                values["page_ids_json"], values["trust_status"], _now(), operator_id,
                values["version"], change_summary.strip(), *self.scope.sql_parameters()))
            self._audit(db, skill_id, version, "create_version", values["version"],
                        "draft", operator_id, reason=change_summary.strip())
        return self.get(skill_id)

    def install_action(self, skill_id: str, operation: str, operator_id: str,
                       version: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            latest = self._version(db, skill_id)
            installation=db.execute("""SELECT * FROM formal_agent_skill_installations WHERE skill_id=?
              AND tenant_id=? AND workspace_id=?""",(skill_id,*self.scope.sql_parameters())).fetchone()
            if not installation:
                raise SkillLifecycleError(
                    "installation_not_found", skill_id=skill_id,
                    version=version or latest["version"],
                    lifecycle_status=latest["version_status"],
                    required_action="install")
            before=installation["installation_status"]
            current_version=installation["installed_version"]
            if operation=="install":
                target=self._version(db,skill_id,version)
                if target["version_status"] == "draft":
                    raise SkillLifecycleError(
                        "skill_not_validated", skill_id=skill_id, version=target["version"],
                        lifecycle_status="draft", installation_status=before,
                        required_action="validate")
                if target["version_status"] != "published":
                    raise SkillLifecycleError(
                        "skill_not_published", skill_id=skill_id, version=target["version"],
                        lifecycle_status=target["version_status"], installation_status=before,
                        required_action="publish")
                if before not in {"not_installed","uninstalled"}:
                    raise SkillLifecycleError(
                        "skill_install_transition_invalid", skill_id=skill_id,
                        version=target["version"], lifecycle_status=target["version_status"],
                        installation_status=before)
                after="installed"; target_version=target["version"]
            elif operation=="enable":
                if before in {"not_installed", "uninstalled"}:
                    raise SkillLifecycleError(
                        "skill_not_installed", skill_id=skill_id, version=latest["version"],
                        lifecycle_status=latest["version_status"], installation_status=before,
                        required_action="install")
                if before not in {"installed","disabled"}:
                    raise SkillLifecycleError(
                        "skill_enable_transition_invalid", skill_id=skill_id,
                        version=current_version, installation_status=before,
                        required_action="enable")
                after="enabled"; target_version=current_version
            elif operation=="disable":
                if before!="enabled": raise SkillLifecycleError("skill_disable_transition_invalid")
                after="disabled"; target_version=current_version
            elif operation=="uninstall":
                if before not in {"installed","disabled"}: raise SkillLifecycleError("skill_uninstall_transition_invalid")
                after="uninstalled"; target_version=current_version
            elif operation in {"upgrade","rollback"}:
                target=self._version(db,skill_id,version)
                if before not in {"installed","enabled","disabled"} or target["version_status"]!="published":
                    raise SkillLifecycleError("skill_install_version_transition_invalid")
                after=before; target_version=target["version"]
            else: raise SkillLifecycleError("skill_install_operation_invalid")
            db.execute("""UPDATE formal_agent_skill_installations SET previous_version=installed_version,
              installed_version=?,installation_status=?,installed_at=COALESCE(installed_at,?),updated_at=?,operator_id=?
              WHERE installation_id=?""",(target_version,after,_now(),_now(),operator_id,installation["installation_id"]))
            self._audit(db,skill_id,target_version,operation,before,after,operator_id)
        return self.get(skill_id)

    def prepare_invocation(self, skill_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        with self.connect() as db:
            latest = self._version(db, skill_id)
            installation=db.execute("""SELECT * FROM formal_agent_skill_installations WHERE skill_id=?
              AND tenant_id=? AND workspace_id=?""",(skill_id,*self.scope.sql_parameters())).fetchone()
            if not installation:
                raise SkillLifecycleError(
                    "installation_not_found", skill_id=skill_id, version=latest["version"],
                    lifecycle_status=latest["version_status"], required_action="install")
            installation_status = installation["installation_status"]
            if installation_status != "enabled":
                if installation_status in {"not_installed", "uninstalled"}:
                    if latest["version_status"] == "draft":
                        code, action = "skill_not_validated", "validate"
                    elif latest["version_status"] == "validated":
                        code, action = "skill_not_published", "publish"
                    else:
                        code, action = "skill_not_installed", "install"
                elif installation_status == "disabled":
                    code, action = "skill_disabled", "enable"
                else:
                    code, action = "skill_not_enabled", "enable"
                raise SkillLifecycleError(
                    code, skill_id=skill_id,
                    version=installation["installed_version"] or latest["version"],
                    lifecycle_status=latest["version_status"],
                    installation_status=installation_status,
                    required_action=action)
            version=self._version(db,skill_id,installation["installed_version"])
        item=self._decode(version)
        with self.connect() as db:
            source=db.execute("""SELECT content_text FROM formal_agent_skill_sources WHERE skill_id=?
              AND tenant_id=? AND workspace_id=?""",(skill_id,*self.scope.sql_parameters())).fetchone()
        item["instructions"] = str(source["content_text"] if source else "")
        try:
            schema_arguments=arguments
            if item.get("binding")=="instruction_only":
                allowed=set((item.get("input_schema") or {}).get("properties") or {})
                schema_arguments={key:value for key,value in arguments.items() if key in allowed}
            validate_schema(schema_arguments, item["input_schema"])
        except FormalAgentSkillError as exc:
            raise SkillLifecycleError(
                "schema_validation_failed", skill_id=skill_id, version=item["version"],
                lifecycle_status=item["version_status"],
                installation_status="enabled", required_action="correct_input",
                safe_detail={"validation_code": str(exc)}) from exc
        if skill_id == "classify-provider-error" and not (
                str(arguments.get("request_id") or "").strip()
                or str(arguments.get("error_id") or "").strip()):
            raise SkillLifecycleError("request_id_or_error_id_required")
        if item["confirmation_required"] and arguments.get("confirmed") is not True:
            raise SkillLifecycleError("skill_confirmation_required")
        if "production_change" in item["requested_permissions"]:
            raise SkillLifecycleError("skill_production_change_forbidden")
        return item

    def record_invocation(self, *, invocation_id: str, audit_id: str, operator_id: str,
                          skill: Mapping[str, Any], started_at: str, result: Mapping[str, Any],
                          permission_decision: str, execution_result: str,
                          arguments: Mapping[str, Any]) -> None:
        finished=_now(); network=bool(result.get("network_called")); write=bool(result.get("write_performed"))
        with self.connect() as db:
            db.execute("""INSERT INTO formal_agent_skill_invocations(
              invocation_id,audit_id,operator_id,skill_id,skill_version,started_at,
              finished_at,permission_decision,execution_result,request_id,response_id,
              decision_id,network_called,write_performed,input_sha256,safe_result_json,
              tenant_id,workspace_id,execution_engine,model_id,host_id,input_summary,
              result_summary,latency_ms,input_tokens,output_tokens) VALUES(
              ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
                invocation_id,audit_id,operator_id,skill["skill_id"],skill["version"],started_at,finished,
                permission_decision,execution_result,result.get("request_id"),result.get("response_id"),
                result.get("decision_id") or arguments.get("decision_id"),int(network),int(write),
                _sha(_canonical(arguments)),_canonical(result),*self.scope.sql_parameters(),
                result.get("execution_engine"),result.get("model_id"),result.get("host_id"),
                str(arguments.get("task") or "")[:240],str(result.get("result_summary") or "")[:500],
                result.get("latency_ms"),result.get("input_tokens"),result.get("output_tokens")))

    def invocations(self, skill_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1<=limit<=500: raise SkillLifecycleError("skill_invocation_limit_invalid")
        query="SELECT * FROM formal_agent_skill_invocations WHERE tenant_id=? AND workspace_id=?"
        args:list[Any]=list(self.scope.sql_parameters())
        if skill_id: query+=" AND skill_id=?"; args.append(skill_id)
        query+=" ORDER BY started_at DESC LIMIT ?"; args.append(limit)
        with self.connect() as db:
            rows=[]
            for row in db.execute(query,args):
                item=dict(row)
                try: item["data"]=json.loads(item.pop("safe_result_json"))
                except (TypeError,ValueError,json.JSONDecodeError): item["data"]={}
                rows.append(item)
            return rows

    def audits(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM formal_agent_skill_lifecycle_audit
              WHERE tenant_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT ?""",
              (*self.scope.sql_parameters(),limit))]

    def hosts(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM formal_agent_skill_hosts
              WHERE tenant_id=? AND workspace_id=? ORDER BY host_name""",self.scope.sql_parameters())]
