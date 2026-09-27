"""Fail-closed Weimeta UAT execution, evidence storage, and correlation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from execution_guard import ExecutionGuard
from backend.platform_environments import load_platform_environments
from backend.tenant_security import TenantScope

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "config" / "uat_execution_policy_v1.json"
PARSER_VERSION = "uat-response-parser-v2.0.0"
PARSER_VERSION_BEFORE = "uat-response-parser-v1.0.0"
MAX_PARSE_BYTES = 1_000_000
SENSITIVE = re.compile(
    r"(?i)(authorization\s*[:=]|bearer\s+[A-Za-z0-9._~+/-]{8,}|sk-[A-Za-z0-9_-]{8,}|"
    r"(?:api[_-]?key|token|secret|credential|authorization)[\"']?\s*[:=]|"
    r"cookie\s*[:=]|\bpassword[\"']?\s*[:=])"
)


def _nullable_limit(name: str, policy_key: str, caster: Callable[[Any], Any]) -> Any | None:
    """Load an optional local limit; empty/null explicitly means disabled."""
    raw = os.getenv(name)
    if raw is None:
        raw = json.loads(POLICY_PATH.read_text(encoding="utf-8")).get(policy_key)
    if raw is None or (isinstance(raw, str) and raw.strip().lower() in {"", "null", "none"}):
        return None
    return caster(raw)
ERROR_CONTRACT = {
    "local_json_error": ("local", False, False, "修正本地 JSON", False),
    "local_encoding_error": ("local", False, False, "修正字符编码", False),
    "client_schema_error": ("client", False, False, "修正客户端结构", False),
    "user_parameter_error": ("user", False, False, "修正请求参数", True),
    "user_authentication_error": ("user", False, False, "核查用户凭证", True),
    "platform_authentication_error": ("platform", False, False, "联系平台管理员", True),
    "upstream_channel_authentication_error": ("upstream", False, True, "由渠道运维核查", True),
    "model_not_available": ("routing", False, True, "选择已确认模型", True),
    "channel_not_available": ("routing", False, True, "等待渠道恢复", True),
    "rate_limited": ("upstream", True, True, "按审批策略有限回退", True),
    "upstream_5xx": ("upstream", True, True, "保留证据并有限回退", True),
    "gateway_502": ("gateway", True, True, "核查 HTML 网关证据", True),
    "gateway_timeout": ("gateway", True, True, "核查网关超时", True),
    "network_timeout": ("transport", True, True, "核查网络超时", True),
    "protocol_conversion_error": ("protocol", False, True, "核查协议转换", True),
    "sse_incomplete": ("protocol", False, False, "保留部分输出且不得拼接重试", True),
    "usage_or_cost_mismatch": ("billing", False, False, "核对 usage 与账单证据", True),
    "unknown": ("unknown", False, False, "人工检查脱敏证据", True),
}


def runtime_error(category: str, summary: str = "") -> dict:
    category = category if category in ERROR_CONTRACT else "unknown"
    layer, retryable, fallback, action, evidence = ERROR_CONTRACT[category]
    return {
        "error_category": category, "error_layer": layer, "retryable": retryable,
        "fallback_allowed": fallback, "recommended_action": action,
        "maximum_total_attempts": 1, "evidence_required": evidence,
        "safe_summary": redact(summary)[:1000],
    }


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> str:
    return SENSITIVE.sub("[REDACTED]", str(value or ""))


@dataclass(frozen=True)
class UatSettings:
    environment: str
    base_url: str
    api_key: str
    enabled: bool
    allowed_hosts: tuple[str, ...]
    timeout_seconds: int
    max_tokens: int | None
    daily_request_limit: int | None
    daily_budget_cny: float | None
    max_request_cost_cny: float
    environment_id: str = "china_uat"
    display_name: str = "国内 UAT"
    currency: str | None = "CNY"
    completion_url: str | None = "https://api-uat.weimeta.cn/v1/chat/completions"
    configuration_complete: bool = True
    environment_execution_supported: bool = True

    @classmethod
    def load(cls, environment_id: str = "china_uat") -> "UatSettings":
        environment = load_platform_environments().require(environment_id)
        prefix = "WEIMETA_CHINA_UAT" if environment_id == "china_uat" else "WEIMETA_OVERSEAS"
        legacy_key = os.getenv("WEIMETA_UAT_API_KEY", "") if environment_id == "china_uat" else ""
        legacy_enabled = os.getenv("WEIMETA_REAL_EXECUTION_ENABLED", "false") if environment_id == "china_uat" else "false"
        return cls(
            environment="uat" if environment_id == "china_uat" else environment_id,
            base_url=environment.api_base_url or "",
            api_key=os.getenv(f"{prefix}_API_KEY", legacy_key),
            enabled=environment.real_execution_supported and os.getenv(f"{prefix}_REAL_EXECUTION_ENABLED", legacy_enabled).lower() == "true",
            allowed_hosts=environment.allowed_api_hosts,
            timeout_seconds=int(os.getenv("WEIMETA_REQUEST_TIMEOUT_SECONDS", "30")),
            max_tokens=(int(os.environ["WEIMETA_MAX_TOKENS_LIMIT"])
                        if os.getenv("WEIMETA_MAX_TOKENS_LIMIT", "").strip()
                        else None),
            daily_request_limit=_nullable_limit("WEIMETA_DAILY_REQUEST_LIMIT", "daily_request_limit", int),
            daily_budget_cny=_nullable_limit("WEIMETA_DAILY_BUDGET_CNY", "daily_budget_cny", float),
            max_request_cost_cny=float(os.getenv("WEIMETA_MAX_REQUEST_COST_CNY", "0.20")),
            environment_id=environment_id, display_name=environment.display_name,
            currency=environment.currency, completion_url=environment.chat_completions_url(),
            configuration_complete=environment.configuration_complete,
            environment_execution_supported=environment.real_execution_supported,
        )


class UatStore:
    def __init__(self, path: Path, *, tenant_scope: TenantScope | None = None):
        self.path = path
        # Construction without an explicit scope is retained only for the
        # established local-development/test path.  Enterprise request paths
        # construct a request-scoped store from a verified PrincipalContext.
        self.scope = tenant_scope or TenantScope.local_development()
        self.init()

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def init(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS uat_executions(
              execution_id TEXT PRIMARY KEY, local_request_id TEXT UNIQUE NOT NULL,
              decision_id TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL,
              estimated_cost_cny REAL NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uat_request_evidence(
              execution_id TEXT PRIMARY KEY REFERENCES uat_executions(execution_id),
              evidence_sha256 TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS uat_response_evidence(
              execution_id TEXT PRIMARY KEY REFERENCES uat_executions(execution_id),
              evidence_sha256 TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow_decisions(
              decision_id TEXT PRIMARY KEY, execution_id TEXT UNIQUE REFERENCES uat_executions(execution_id),
              created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS route_correlations(
              correlation_id TEXT PRIMARY KEY, execution_id TEXT UNIQUE REFERENCES uat_executions(execution_id),
              created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_budget_usage(
              usage_date TEXT PRIMARY KEY, request_count INTEGER NOT NULL, estimated_cost_cny REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_events(
              event_id TEXT PRIMARY KEY, execution_id TEXT, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, actor TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence_amendments(
              amendment_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, reason TEXT NOT NULL,
              actor TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imported_log_records(
              record_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, source_sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS environment_endpoint_validations(
              validation_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL,
              validation_type TEXT NOT NULL, created_at TEXT NOT NULL,
              success INTEGER NOT NULL, payload TEXT NOT NULL);
            """)
            environment_tables = (
                "uat_executions", "uat_request_evidence", "uat_response_evidence",
                "shadow_decisions", "route_correlations", "audit_events",
                "evidence_amendments", "imported_log_records",
            )
            for table in environment_tables:
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "environment_id" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN environment_id TEXT NOT NULL DEFAULT 'china_uat'")
            execution_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(uat_executions)")}
            for name in (
                "provider_request_id", "provider_response_id",
                "provider_trace_id", "client_correlation_id",
            ):
                if name not in execution_columns:
                    db.execute(
                        f"ALTER TABLE uat_executions ADD COLUMN {name} TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS daily_budget_usage_by_environment(
              environment_id TEXT NOT NULL, usage_date TEXT NOT NULL, request_count INTEGER NOT NULL,
              estimated_cost REAL NOT NULL, currency TEXT, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,environment_id,usage_date))""")
            tenant_tables = environment_tables + (
                "daily_budget_usage", "daily_budget_usage_by_environment",
                "environment_endpoint_validations",
            )
            for table in tenant_tables:
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if "tenant_id" not in columns:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN tenant_id TEXT NOT NULL "
                        f"DEFAULT '{self.scope.tenant_id}'"
                    )
                if "workspace_id" not in columns:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL "
                        f"DEFAULT '{self.scope.workspace_id}'"
                    )
            db.execute("""INSERT OR IGNORE INTO daily_budget_usage_by_environment(
              environment_id,usage_date,request_count,estimated_cost,currency,tenant_id,workspace_id)
              SELECT 'china_uat',usage_date,request_count,estimated_cost_cny,'CNY',tenant_id,workspace_id
              FROM daily_budget_usage
              WHERE tenant_id=? AND workspace_id=?""", self.scope.sql_parameters())

    def save_endpoint_validation(self, evidence: dict) -> None:
        payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        with self.connect() as db:
            db.execute("""INSERT INTO environment_endpoint_validations(
              validation_id,environment_id,validation_type,created_at,success,payload,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?)""", (
                evidence["validation_id"], evidence["environment_id"],
                evidence["validation_type"], evidence["validation_timestamp"],
                1 if evidence["success"] else 0, payload,
                *self.scope.sql_parameters(),
            ))

    def latest_successful_endpoint_validation(
        self, environment_id: str, validation_type: str
    ) -> dict | None:
        with self.connect() as db:
            row = db.execute("""SELECT payload FROM environment_endpoint_validations
              WHERE environment_id=? AND validation_type=? AND success=1
              AND tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC LIMIT 1""",
              (environment_id, validation_type, *self.scope.sql_parameters())).fetchone()
        return json.loads(row[0]) if row else None

    def usage_today(self, environment_id: str = "china_uat") -> tuple[int, float]:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            row = db.execute("""SELECT request_count,estimated_cost FROM daily_budget_usage_by_environment
              WHERE environment_id=? AND usage_date=? AND tenant_id=? AND workspace_id=?""",
              (environment_id, day, *self.scope.sql_parameters())).fetchone()
            if row is None and environment_id == "china_uat":
                row = db.execute("""SELECT request_count,estimated_cost_cny FROM daily_budget_usage
                  WHERE usage_date=? AND tenant_id=? AND workspace_id=?""",
                  (day, *self.scope.sql_parameters())).fetchone()
        return (int(row[0]), float(row[1])) if row else (0, 0.0)

    def save_execution(self, execution: dict, request_evidence: dict, shadow: dict):
        canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
        environment_id = execution.get("environment_id", "china_uat")
        with self.connect() as db:
            db.execute("""INSERT INTO uat_executions(
              execution_id,local_request_id,decision_id,created_at,status,estimated_cost_cny,payload,environment_id,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                execution["execution_id"], execution["local_request_id"], execution["decision_id"],
                execution["created_at"], execution["status"], execution["estimated_cost_cny"], canonical(execution), environment_id,
                *self.scope.sql_parameters()))
            request_json = canonical(request_evidence)
            db.execute("""INSERT INTO uat_request_evidence(execution_id,evidence_sha256,payload,environment_id,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?)""", (execution["execution_id"], hashlib.sha256(request_json.encode()).hexdigest(), request_json, environment_id,
              *self.scope.sql_parameters()))
            db.execute("""INSERT INTO shadow_decisions(decision_id,execution_id,created_at,payload,environment_id,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?)""", (execution["decision_id"], execution["execution_id"], execution["created_at"], canonical(shadow), environment_id,
              *self.scope.sql_parameters()))

    def finish(self, execution: dict, response: dict):
        canonical = json.dumps(response, ensure_ascii=False, sort_keys=True)
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            db.execute("""UPDATE uat_executions SET status=?,payload=?,
              provider_request_id=COALESCE(?,provider_request_id),
              provider_response_id=COALESCE(?,provider_response_id),
              provider_trace_id=COALESCE(?,provider_trace_id),
              client_correlation_id=COALESCE(?,client_correlation_id)
              WHERE execution_id=?
              AND tenant_id=? AND workspace_id=?""", (
                execution["status"], json.dumps(execution, ensure_ascii=False, sort_keys=True),
                execution.get("provider_request_id"),
                execution.get("provider_response_id"),
                execution.get("provider_trace_id"),
                execution.get("client_correlation_id"), execution["execution_id"],
                *self.scope.sql_parameters()))
            environment_id = execution.get("environment_id", "china_uat")
            db.execute("""INSERT INTO uat_response_evidence(execution_id,evidence_sha256,payload,environment_id,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?)""", (execution["execution_id"], hashlib.sha256(canonical.encode()).hexdigest(), canonical, environment_id,
              *self.scope.sql_parameters()))
            db.execute("""INSERT INTO daily_budget_usage_by_environment(
              environment_id,usage_date,request_count,estimated_cost,currency,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(tenant_id,workspace_id,environment_id,usage_date) DO UPDATE SET request_count=request_count+1,
              estimated_cost=estimated_cost+excluded.estimated_cost""",
              (environment_id, day, 1, execution["estimated_cost_cny"], execution.get("currency"), *self.scope.sql_parameters()))
            db.execute("""INSERT INTO audit_events(event_id,execution_id,event_type,created_at,actor,details,environment_id,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?)""", (
                str(uuid.uuid4()), execution["execution_id"], "uat_execution_completed", now(), "system",
                json.dumps({"status": execution["status"]}), environment_id, *self.scope.sql_parameters()))

    def list(self, environment_id: str | None = None) -> list[dict]:
        with self.connect() as db:
            query = "SELECT payload FROM uat_executions WHERE tenant_id=? AND workspace_id=?"
            params: tuple = self.scope.sql_parameters()
            if environment_id:
                query += " AND environment_id=?"; params = (*params, environment_id)
            return [json.loads(r[0]) for r in db.execute(query + " ORDER BY created_at DESC", params)]

    def get(self, execution_id: str) -> dict | None:
        with self.connect() as db:
            scoped = (execution_id, *self.scope.sql_parameters())
            where = "execution_id=? AND tenant_id=? AND workspace_id=?"
            row = db.execute(f"SELECT payload FROM uat_executions WHERE {where}", scoped).fetchone()
            response = db.execute(f"SELECT payload FROM uat_response_evidence WHERE {where}", scoped).fetchone()
            shadow = db.execute(f"SELECT payload FROM shadow_decisions WHERE {where}", scoped).fetchone()
            correlation = db.execute(f"SELECT payload FROM route_correlations WHERE {where}", scoped).fetchone()
            amendments = [json.loads(r[0]) for r in db.execute(
                f"SELECT payload FROM evidence_amendments WHERE {where} ORDER BY created_at", scoped)]
        if not row:
            return None
        item = json.loads(row[0])
        item["response"] = json.loads(response[0]) if response else None
        item["shadow_decision"] = json.loads(shadow[0]) if shadow else None
        item["correlation"] = json.loads(correlation[0]) if correlation else None
        item["amendments"] = amendments
        item["derived_response"] = amendments[-1]["parsed_fields"] if amendments else None
        return item

    def response_evidence(self, execution_id: str) -> tuple[str, dict] | None:
        with self.connect() as db:
            row = db.execute("""SELECT evidence_sha256,payload FROM uat_response_evidence
              WHERE execution_id=? AND tenant_id=? AND workspace_id=?""",
              (execution_id, *self.scope.sql_parameters())).fetchone()
        return (str(row[0]), json.loads(row[1])) if row else None

    def create_parsing_amendment(self, execution_id: str, expected_sha256: str, parsed_fields: dict, reason: str) -> dict:
        evidence = self.response_evidence(execution_id)
        if not evidence:
            raise ValueError("response_evidence_not_found")
        actual_sha, _ = evidence
        if actual_sha != expected_sha256:
            raise ValueError("original_evidence_sha256_mismatch")
        amendment_id = "AMD-" + hashlib.sha256(f"{execution_id}:{actual_sha}:{PARSER_VERSION}".encode()).hexdigest()[:16].upper()
        amendment = {
            "amendment_id": amendment_id, "execution_id": execution_id,
            "parser_version_before": PARSER_VERSION_BEFORE, "parser_version_after": PARSER_VERSION,
            "original_evidence_sha256": actual_sha, "amendment_reason": reason,
            "parsed_fields": parsed_fields, "created_at": now(),
            "source_type": "derived_from_existing_genuine_evidence",
        }
        with self.connect() as db:
            existing = db.execute("""SELECT payload FROM evidence_amendments WHERE amendment_id=?
              AND tenant_id=? AND workspace_id=?""", (amendment_id, *self.scope.sql_parameters())).fetchone()
            if existing:
                return json.loads(existing[0])
            environment_row=db.execute("""SELECT environment_id FROM uat_executions WHERE execution_id=?
              AND tenant_id=? AND workspace_id=?""",(execution_id, *self.scope.sql_parameters())).fetchone()
            environment_id=str(environment_row[0]) if environment_row else "china_uat"
            db.execute("""INSERT INTO evidence_amendments(
              amendment_id,execution_id,reason,actor,created_at,payload,environment_id,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)""", (
                amendment_id, execution_id, reason, "offline_response_parser", amendment["created_at"],
                json.dumps(amendment, ensure_ascii=False, sort_keys=True),environment_id, *self.scope.sql_parameters()))
            db.execute("""INSERT INTO audit_events(
              event_id,execution_id,event_type,created_at,actor,details,environment_id,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)""", (
                str(uuid.uuid4()), execution_id, "response_parsing_amendment_created",
                amendment["created_at"], "offline_response_parser",
                json.dumps({"amendment_id": amendment_id, "original_evidence_sha256": actual_sha}),environment_id,
                *self.scope.sql_parameters()))
        return amendment

    def save_correlation(self, correlation: dict):
        with self.connect() as db:
            environment_row=db.execute("""SELECT environment_id FROM uat_executions WHERE execution_id=?
              AND tenant_id=? AND workspace_id=?""",(correlation["execution_id"], *self.scope.sql_parameters())).fetchone()
            environment_id=str(environment_row[0]) if environment_row else str(correlation.get("environment_id") or "china_uat")
            db.execute("""INSERT INTO route_correlations(
              correlation_id,execution_id,created_at,payload,environment_id,tenant_id,workspace_id) VALUES(?,?,?,?,?,?,?)
              ON CONFLICT(tenant_id,workspace_id,execution_id) DO UPDATE SET created_at=excluded.created_at,payload=excluded.payload""",
              (str(uuid.uuid4()), correlation["execution_id"], now(), json.dumps(correlation, ensure_ascii=False, sort_keys=True),environment_id,
               *self.scope.sql_parameters()))


def policy() -> dict:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def estimate_cost(messages: list[dict], max_tokens: int) -> dict:
    input_chars = sum(len(str(m.get("content", ""))) for m in messages)
    input_tokens = max(1, (input_chars + 3) // 4)
    cost = round((input_tokens * 1.0 + max_tokens * 2.0) / 1_000_000, 6)
    return {"estimated_input_tokens": input_tokens, "maximum_output_tokens": max_tokens, "estimated_cost_cny": cost, "actual_cost_available": False}


def status(settings: UatSettings, store: UatStore, credential_source: str | None = None) -> dict:
    used, cost = store.usage_today(settings.environment_id)
    parsed = urlparse(settings.base_url)
    request_limit_enabled = settings.daily_request_limit is not None
    budget_limit_enabled = settings.daily_budget_cny is not None
    reasons = []
    if not settings.configuration_complete and settings.environment_id != "overseas":
        reasons.append("environment_configuration_incomplete")
    if not settings.api_key: reasons.append("environment_key_not_configured" if settings.environment_id != "china_uat" else "uat_key_not_configured")
    if not settings.environment_execution_supported:
        reasons.append("overseas_completion_not_yet_authorized" if settings.environment_id == "overseas" else "environment_real_execution_disabled")
    elif not settings.enabled: reasons.append("real_execution_disabled")
    if parsed.scheme != "https" or parsed.hostname not in settings.allowed_hosts: reasons.append("uat_host_not_allowed")
    if request_limit_enabled and used >= settings.daily_request_limit: reasons.append("daily_request_limit_reached")
    if budget_limit_enabled and cost >= settings.daily_budget_cny: reasons.append("daily_budget_exceeded")
    return {
        "environment": settings.environment, "environment_id": settings.environment_id,
        "environment_name": settings.display_name, "base_url": settings.base_url or None,
        "currency": settings.currency, "configuration_complete": settings.configuration_complete,
        "key_configured": bool(settings.api_key), "real_execution_enabled": settings.enabled,
        "credential_source": credential_source or ("environment" if settings.api_key else "none"),
        "daily_request_limit": settings.daily_request_limit, "daily_budget_cny": settings.daily_budget_cny,
        "request_limit_enabled": request_limit_enabled, "budget_limit_enabled": budget_limit_enabled,
        "remaining_budget_cny": max(settings.daily_budget_cny - cost, 0.0) if budget_limit_enabled else None,
        "requests_used_today": used, "estimated_cost_used_today": cost,
        "execution_ready": not reasons, "blocking_reasons": reasons,
    }


def validate(
    body: dict, settings: UatSettings, store: UatStore,
    model_catalog: dict[str, bool] | None = None,
    runtime_constraints: dict[str, Any] | None = None,
) -> dict:
    errors = list(status(settings, store)["blocking_reasons"])
    parsed = urlparse(settings.base_url)
    request = body.get("request") or {}
    confirmation = body.get("confirmation") or {}
    cfg = policy()
    if settings.environment != cfg["allowed_environment"]: errors.append("uat_environment_not_allowed")
    if body.get("mode") != "real_uat_execute": errors.append("invalid_execution_mode")
    if not confirmation.get("confirmed") or confirmation.get("confirmation_text") != "I understand this will call the Weimeta UAT API and may incur UAT cost.": errors.append("explicit_confirmation_required")
    selected_model = request.get("requested_model")
    if model_catalog is None:
        errors.append("uat_model_catalog_unavailable")
        model_allowed = False
    elif selected_model not in model_catalog:
        errors.append("selected_model_not_available")
        model_allowed = False
    elif not model_catalog[selected_model]:
        errors.append("selected_model_not_authorized")
        model_allowed = False
    else:
        model_allowed = True
    try:
        max_tokens = int(request.get("max_tokens") or 0)
    except (TypeError, ValueError):
        max_tokens = 0
    catalog_entry = model_catalog.get(selected_model) if model_catalog else None
    capability = catalog_entry if isinstance(catalog_entry, dict) else {}
    model_limit = capability.get("confirmed_max_output_tokens")
    channel_limit = capability.get("confirmed_channel_max_output_tokens")
    context_limit = capability.get("max_context_tokens")
    input_limit = capability.get("max_input_tokens")
    if max_tokens <= 0:
        errors.append("max_tokens_must_be_positive")
    if model_limit is None:
        errors.append("model_output_limit_unconfirmed")
    if channel_limit is None:
        errors.append("channel_output_limit_unconfirmed")
    messages = request.get("messages") or []
    serialized = json.dumps(messages, ensure_ascii=False)
    if len(serialized) > cfg["maximum_input_characters"]: errors.append("input_too_large")
    if SENSITIVE.search(serialized): errors.append("sensitive_input_rejected")
    if request.get("stream") and not cfg["streaming_enabled"]: errors.append("stream_execution_not_ready")
    estimate = estimate_cost(messages, max_tokens)
    input_tokens = int(estimate["estimated_input_tokens"])
    if isinstance(input_limit, int) and input_tokens > input_limit:
        errors.append("model_input_limit_exceeded")
    elif input_limit is None:
        errors.append("model_input_limit_unconfirmed")
    remaining_context = (context_limit - input_tokens
                         if isinstance(context_limit, int) and context_limit > input_tokens
                         else 0 if isinstance(context_limit, int) else None)
    runtime_constraints = runtime_constraints or {}
    remaining_budget = runtime_constraints.get("remaining_budget_cny")
    affordable_output = None
    if remaining_budget is not None:
        try:
            budget = max(float(remaining_budget), 0.0)
            input_cost = input_tokens / 1_000_000
            affordable_output = max(int((budget - input_cost) * 1_000_000 / 2), 0)
        except (TypeError, ValueError):
            affordable_output = 0
    boundaries = {
        "user_requested_max_tokens": max_tokens if max_tokens > 0 else None,
        "model_max_output_tokens": model_limit,
        "channel_max_output_tokens": channel_limit,
        "remaining_context_capacity": remaining_context,
        "budget_affordable_output_tokens": affordable_output,
    }
    confirmed_values = [value for value in boundaries.values()
                        if isinstance(value, int) and value >= 0]
    effective_max_tokens = min(confirmed_values) if len(confirmed_values) == len(boundaries) else None
    limiting_factor = None
    if model_limit is None: limiting_factor = "model_capability_unconfirmed"
    elif channel_limit is None: limiting_factor = "channel_capability_unconfirmed"
    elif remaining_context is None: limiting_factor = "context_capacity_unconfirmed"
    elif affordable_output is None: limiting_factor = "execution_budget_unconfirmed"
    elif effective_max_tokens is not None:
        limiting_factor = next((name for name, value in boundaries.items()
          if value == effective_max_tokens), None)
    if context_limit is None: errors.append("context_capacity_unconfirmed")
    elif remaining_context == 0: errors.append("context_capacity_exhausted")
    if affordable_output is None: errors.append("execution_budget_unconfirmed")
    elif affordable_output < max_tokens: errors.append("execution_budget_output_limit_exceeded")
    if model_limit is not None and max_tokens > model_limit:
        errors.append("model_output_limit_exceeded")
    if channel_limit is not None and max_tokens > channel_limit:
        errors.append("channel_output_limit_exceeded")
    if remaining_context is not None and max_tokens > remaining_context:
        errors.append("context_capacity_exceeded")
    used, spent = store.usage_today(settings.environment_id)
    if estimate["estimated_cost_cny"] > settings.max_request_cost_cny: errors.append("single_request_cost_limit_exceeded")
    if settings.daily_budget_cny is not None and spent + estimate["estimated_cost_cny"] > settings.daily_budget_cny: errors.append("daily_budget_exceeded")
    if settings.daily_request_limit is not None and used >= settings.daily_request_limit: errors.append("daily_request_limit_reached")
    guard = ExecutionGuard.check_uat({
        "environment_supported": settings.environment_id in {"china_uat", "overseas"},
        "environment_configured": settings.configuration_complete or settings.environment_id == "overseas",
        "environment_execution_enabled": settings.environment_execution_supported,
        "mode_valid": body.get("mode") == "real_uat_execute",
        "execution_enabled": settings.enabled,
        "key_configured": bool(settings.api_key),
        "host_allowed": parsed.scheme == "https" and parsed.hostname in settings.allowed_hosts,
        "explicit_confirmation": bool(confirmation.get("confirmed")) and confirmation.get("confirmation_text") == "I understand this will call the Weimeta UAT API and may incur UAT cost.",
        "model_allowed": model_allowed,
        "token_limit_ok": 0 < max_tokens and effective_max_tokens == max_tokens,
        "request_limit_ok": settings.daily_request_limit is None or used < settings.daily_request_limit,
        "budget_ok": settings.daily_budget_cny is None or spent + estimate["estimated_cost_cny"] <= settings.daily_budget_cny,
        "request_cost_ok": estimate["estimated_cost_cny"] <= settings.max_request_cost_cny,
        "input_safe": not bool(SENSITIVE.search(serialized)),
        "stream_supported": not request.get("stream") or cfg["streaming_enabled"],
        "stop_rule_clear": True,
    })
    specific_errors = {"uat_model_catalog_unavailable", "selected_model_not_available", "selected_model_not_authorized",
      "overseas_endpoint_unconfirmed", "overseas_execution_not_authorized",
      "overseas_completion_not_yet_authorized", "environment_configuration_incomplete"}
    if guard.status == "blocked" and guard.error_category and not specific_errors.intersection(errors):
        errors.insert(0, guard.error_category)
    return {"valid": not errors, "guard_status": guard.status,
      "blocking_reasons": list(dict.fromkeys(errors)), "cost_estimate": estimate,
      "policy_version": cfg["policy_version"],
      "token_boundary": {
        "user_requested_max_tokens": max_tokens,
        "model_max_output_tokens": model_limit,
        "channel_max_output_tokens": channel_limit,
        "model_max_input_tokens": input_limit,
        "remaining_context_capacity": remaining_context,
        "budget_affordable_output_tokens": affordable_output,
        "effective_max_tokens": effective_max_tokens,
        "limiting_factor": limiting_factor,
        "evidence_source": capability.get("evidence_source", "pending_confirmation"),
        "evidence_version": capability.get("evidence_version"),
        "fresh_until": capability.get("fresh_until"),
      }}


def default_transport(url: str, api_key: str, payload: dict, timeout: int) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            raw = response.read()
            return {"status": response.status, "headers": dict(response.headers), "body": raw, "elapsed_ms": round((time.perf_counter()-started)*1000)}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "headers": dict(exc.headers), "body": exc.read(), "elapsed_ms": round((time.perf_counter()-started)*1000)}


def classify_response(status_code: int | None, content_type: str, body: bytes, timed_out: bool = False) -> dict:
    raw = body[:MAX_PARSE_BYTES]
    decoded = raw.decode("utf-8", errors="replace")
    sensitive = bool(SENSITIVE.search(decoded))
    text = redact(decoded)
    if timed_out:
        category = "network_timeout"
    elif status_code == 400: category = "user_parameter_error"
    elif status_code == 401: category = "platform_authentication_error"
    elif status_code == 429: category = "rate_limited"
    elif status_code == 502 and "html" in content_type.lower(): category = "gateway_502"
    elif status_code and status_code >= 500: category = "upstream_5xx"
    else: category = None
    parsed = None
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    trimmed = text.strip()
    looks_json = trimmed.startswith(("{", "[")) and trimmed.endswith(("}", "]"))
    html = "html" in media_type or trimmed.lower().startswith(("<!doctype html", "<html"))
    should_parse = not sensitive and not html and ("json" in media_type or (media_type in {"", "unknown", "text/plain", "application/octet-stream"} and looks_json))
    if should_parse:
        try: parsed = json.loads(trimmed)
        except json.JSONDecodeError:
            if "json" in media_type and category is None: category = "protocol_conversion_error"
    choice = ((parsed or {}).get("choices") or [{}])[0] if isinstance(parsed, dict) else {}
    usage = (parsed or {}).get("usage") or {} if isinstance(parsed, dict) else {}
    message = choice.get("message") or {} if isinstance(choice, dict) else {}
    choices = parsed.get("choices") if isinstance(parsed, dict) else None
    has_content = bool(message.get("content"))
    response_content_complete = bool(200 <= (status_code or 0) < 300 and parsed is not None and choices and has_content and choice.get("finish_reason"))
    usage_complete = all(usage.get(k) is not None for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
    evidence_metadata_complete = bool(isinstance(parsed, dict) and parsed.get("id") and parsed.get("model"))
    if category is not None or not 200 <= (status_code or 0) < 300:
        completeness = "failed"
    elif parsed is None:
        completeness = "unknown" if not trimmed else "incomplete"
    elif not response_content_complete:
        completeness = "incomplete"
    elif usage_complete and evidence_metadata_complete:
        completeness = "complete"
    else:
        completeness = "complete_with_missing_optional_fields"
    content_type_mismatch = bool(parsed is not None and "json" not in media_type)
    contract = runtime_error(category or "unknown", text) if category else None
    return {
        "http_status": status_code, "content_type": content_type or "unknown",
        "original_content_type": content_type or None,
        "response_body_type": "json" if parsed is not None else "html" if html else "text",
        "parsed_body_type": "json" if parsed is not None else "html" if html else "text",
        "content_type_mismatch": content_type_mismatch,
        "response_id": parsed.get("id") if isinstance(parsed, dict) else None,
        "actual_model": parsed.get("model") if isinstance(parsed, dict) else None,
        "object": parsed.get("object") if isinstance(parsed, dict) else None,
        "created": parsed.get("created") if isinstance(parsed, dict) else None,
        "assistant_content": message.get("content"),
        "reasoning_content": message.get("reasoning_content"),
        "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"), "finish_reason": choice.get("finish_reason"),
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens") if isinstance(usage.get("prompt_tokens_details"), dict) else usage.get("cached_tokens"),
        "usage_details": usage or None,
        "error_summary": text if category else None, "error_category": category,
        "retryable": contract["retryable"] if contract else False,
        "fallback_allowed": contract["fallback_allowed"] if contract else False,
        "error_contract": contract,
        "response_completeness": completeness,
        "response_content_complete": response_content_complete,
        "evidence_metadata_complete": evidence_metadata_complete,
        "usage_complete": usage_complete,
        "correlation_ready": False,
        "parser_version": PARSER_VERSION,
        "safe_response": parsed if parsed is not None else text[:4000],
    }


def reparse_stored_response(store: UatStore, execution_id: str, expected_sha256: str | None = None, confirm: bool = False) -> dict:
    evidence = store.response_evidence(execution_id)
    if not evidence:
        raise ValueError("response_evidence_not_found")
    evidence_sha, original = evidence
    if expected_sha256 and expected_sha256 != evidence_sha:
        raise ValueError("original_evidence_sha256_mismatch")
    safe = original.get("safe_response")
    body = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False)
    parsed = classify_response(original.get("http_status"), original.get("original_content_type") or original.get("content_type") or "", body.encode("utf-8"))
    result = {
        "execution_id": execution_id, "dry_run": not confirm,
        "original_evidence_sha256": evidence_sha,
        "parser_version_before": original.get("parser_version") or PARSER_VERSION_BEFORE,
        "parser_version_after": PARSER_VERSION,
        "before": {k: original.get(k) for k in ("response_body_type", "response_id", "actual_model", "input_tokens", "output_tokens", "total_tokens", "finish_reason", "response_completeness")},
        "after": parsed,
    }
    if confirm:
        result["amendment"] = store.create_parsing_amendment(
            execution_id, evidence_sha, parsed,
            "Re-parse intact stored safe response after case-insensitive Content-Type and JSON-sniffing correction.")
    return result


def execute(body: dict, settings: UatSettings, store: UatStore, shadow_builder: Callable[[dict], dict],
            transport=default_transport, credential_source: str = "environment",
            credential_still_valid: Callable[[], bool] | None = None,
            model_catalog: dict[str, bool] | None = None,
            runtime_constraints: dict[str, Any] | None = None,
            call_logger: Any | None = None) -> dict:
    check = validate(body, settings, store, model_catalog, runtime_constraints)
    if not check["valid"]:
        return {"validation": check, "network_execution": {"execution_attempted": False, "network_called": False}}
    if credential_still_valid is not None and not credential_still_valid():
        check["valid"] = False
        check["guard_status"] = "blocked"
        check["blocking_reasons"] = ["uat_session_credential_expired"]
        return {"validation": check, "network_execution": {"execution_attempted": False, "network_called": False}}
    request = body["request"]
    local_request_id = f"REQ-{uuid.uuid4().hex[:16].upper()}"
    execution_id = f"UAT-{uuid.uuid4().hex[:16].upper()}"
    shadow = shadow_builder({**request, "request_id": local_request_id})
    decision_id = f"DEC-{uuid.uuid4().hex[:16].upper()}"
    shadow["decision_id"] = decision_id
    execution = {
        "execution_id": execution_id, "local_execution_id": execution_id, "local_request_id": local_request_id,
        "decision_id": decision_id, "plan_id": body.get("measurement", {}).get("plan_id"),
        "campaign_version": "unified-routing-v3.0.0", "mode": body["mode"],
        "requested_model": request["requested_model"], "stream": bool(request.get("stream")),
        "max_tokens": request["max_tokens"],
        "user_requested_max_tokens": check["token_boundary"]["user_requested_max_tokens"],
        "effective_max_tokens": check["token_boundary"]["effective_max_tokens"],
        "model_max_output_tokens": check["token_boundary"]["model_max_output_tokens"],
        "channel_max_output_tokens": check["token_boundary"]["channel_max_output_tokens"],
        "remaining_context_capacity": check["token_boundary"]["remaining_context_capacity"],
        "budget_affordable_output_tokens": check["token_boundary"]["budget_affordable_output_tokens"],
        "limiting_factor": check["token_boundary"]["limiting_factor"],
        "prompt_profile_id": body.get("measurement", {}).get("request_profile_id"),
        "request_started_at": now(), "created_at": now(), "policy_version": check["policy_version"],
        "catalog_version": shadow.get("catalog_version"), "catalog_sha256": shadow.get("catalog_sha256"),
        "shadow_recommended_candidate": shadow.get("recommended_candidate"),
        "fallback_order": shadow.get("fallback_order", []), "execution_attempted": True,
        "network_called": False, "target_host": urlparse(settings.completion_url or "").hostname, "environment": settings.environment,
        "environment_id": settings.environment_id, "currency": settings.currency,
        "source_type": "measured_unified_uat", "estimated_cost_cny": check["cost_estimate"]["estimated_cost_cny"],
        "estimated_cost": check["cost_estimate"]["estimated_cost_cny"],
        "actual_cost": None, "actual_output_tokens": None,
        "local_request_limit_enabled": settings.daily_request_limit is not None,
        "local_budget_limit_enabled": settings.daily_budget_cny is not None,
        "status": "executing", "platform_actual_channel": None, "correlation_status": "awaiting_backend_log",
        "credential_source": credential_source,
    }
    request_evidence = {k: v for k, v in request.items() if k != "messages"}
    request_evidence["message_profiles"] = [{"role": m.get("role"), "character_count": len(str(m.get("content", "")))} for m in request["messages"]]
    store.save_execution(execution, request_evidence, shadow)  # shadow is durable before transport
    call_record_id = None
    if call_logger is not None:
        call_record_id = call_logger.start_execution(
            request_id=local_request_id, decision_id=decision_id,
            environment_id=settings.environment_id,
            requested_model=request["requested_model"],
            stream=bool(request.get("stream")),
            channel_id=request.get("channel_id"),
            endpoint_type="openai_compatible",
            configuration_version=check.get("policy_version"),
        )
    try:
        result = transport(settings.completion_url or "", settings.api_key, {
            "model": request["requested_model"], "messages": request["messages"],
            "stream": request.get("stream", False), "max_tokens": request["max_tokens"],
        }, settings.timeout_seconds)
        execution["network_called"] = True
        headers = {str(k).lower(): v for k, v in result.get("headers", {}).items()}
        response = classify_response(result["status"], headers.get("content-type", ""), result.get("body", b""))
        response["request_id_header"] = headers.get("x-request-id")
        response["correlation_ready"] = bool(response["request_id_header"] or response["response_id"])
        response["elapsed_ms"] = result.get("elapsed_ms")
    except (TimeoutError, urllib.error.URLError):
        execution["network_called"] = True
        response = classify_response(None, "", b"", timed_out=True)
    except Exception as exc:
        execution["network_called"] = True
        response = classify_response(None, "", b"")
        contract = runtime_error("unknown", type(exc).__name__)
        response.update({
            "error_category": "unknown",
            "error_summary": type(exc).__name__,
            "retryable": contract["retryable"],
            "fallback_allowed": contract["fallback_allowed"],
            "error_contract": contract,
            "response_completeness": "failed",
        })
    execution["request_completed_at"] = now()
    execution["elapsed_ms"] = response.get("elapsed_ms")
    execution["actual_output_tokens"] = response.get("output_tokens")
    execution["status"] = "succeeded" if response["error_category"] is None else "failed"
    store.finish(execution, response)
    if call_logger is not None and call_record_id is not None:
        call_logger.finish_execution(
            call_record_id,
            status="SUCCESS" if response["error_category"] is None else "FAILED",
            response_id=response.get("response_id"),
            actual_model=response.get("actual_model"),
            http_status=response.get("http_status"),
            error_code=response.get("error_category"),
            error_category=response.get("error_category"),
            retryable=bool(response.get("retryable")),
            total_latency_ms=response.get("elapsed_ms"),
            first_token_latency_ms=response.get("first_token_latency_ms"),
            input_tokens=response.get("input_tokens"),
            cached_input_tokens=response.get("cached_tokens"),
            output_tokens=response.get("output_tokens"),
            cost_amount=execution.get("actual_cost"),
            currency=settings.currency,
        )
    return {"validation": check, "shadow_decision": shadow, "network_execution": {
        "execution_attempted": True, "network_called": True, "target_host": execution["target_host"]},
        "observed_api_result": response, "backend_correlation": {"status": "awaiting_backend_log"},
        "execution": execution, "limitations": ["平台实际渠道只能由导入的后端日志证据确认。"]}


def correlate(execution: dict, logs: list[dict], attached_log_id: str | None = None) -> dict:
    response = execution.get("response") or {}
    request_id = response.get("request_id_header")
    response_id = response.get("response_id")
    if attached_log_id:
        matches = [x for x in logs if x.get("platform_log_id") == attached_log_id]
        method = "operator_attached_platform_log_id"
    elif request_id:
        matches = [x for x in logs if x.get("request_id") == request_id]
        method = "exact_request_id"
    elif response_id:
        matches = [x for x in logs if x.get("response_id") == response_id]
        method = "exact_response_id"
    else:
        matches = []
        method = None
    if not matches:
        state = "awaiting_backend_log" if not logs else "unmatched"
    elif len(matches) > 1:
        state = "ambiguous"
    else:
        state = "exact_match" if method in {"exact_request_id", "exact_response_id"} else "strong_match"
    match = matches[0] if len(matches) == 1 else {}
    return {
        "execution_id": execution["execution_id"], "correlation_status": state,
        "correlation_method": method, "correlation_confidence": 1.0 if state == "exact_match" else .9 if state == "strong_match" else 0,
        "matched_platform_log_id": match.get("platform_log_id"), "matched_request_id": match.get("request_id"),
        "platform_actual_channel": match.get("channel_id"), "timestamp_delta_ms": None,
        "token_match": None, "cost_match": None, "model_match": None,
        "ambiguity_count": len(matches), "human_confirmation_required": state in {"strong_match", "ambiguous"},
        "human_confirmed": False, "confirmed_by": None, "confirmed_at": None,
    }
