from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.browser_import_service import _collector_context
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope

ACTIVE_STATES = {
    "created", "launching_browser", "awaiting_manual_login", "login_confirmed",
    "collecting", "stop_requested",
}
TERMINAL_STATES = {"completed", "stopped", "failed", "browser_closed", "heartbeat_lost"}
TRANSITIONS = {
    "created": {"launching_browser", "failed", "stop_requested"},
    "launching_browser": {"awaiting_manual_login", "failed", "browser_closed", "heartbeat_lost", "stop_requested"},
    "awaiting_manual_login": {"login_confirmed", "failed", "browser_closed", "heartbeat_lost", "stop_requested"},
    "login_confirmed": {"collecting", "failed", "browser_closed", "heartbeat_lost", "stop_requested"},
    "collecting": {"preview_ready", "failed", "browser_closed", "heartbeat_lost", "stop_requested"},
    "preview_ready": {"import_confirmed", "stop_requested"},
    "import_confirmed": {"completed", "failed"},
    "stop_requested": {"stopped", "failed"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_url(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class CollectorRunError(ValueError):
    pass


class CollectorRunService:
    def __init__(self, path: Path, runtime_settings: Any | None = None,
                 authorization: AuthorizationService | None = None):
        self.path = Path(path)
        self.runtime_settings = runtime_settings
        self.authorization = authorization
        self._init()

    def _scope(self, principal: PrincipalContext | None,
               permission: str) -> TenantScope:
        if self.authorization is None:
            if isinstance(principal, PrincipalContext) and principal.is_verified:
                return TenantScope(principal.tenant_id, principal.workspace_id)
            return TenantScope.local_development()
        try:
            verified = self.authorization.authorize(principal, permission)
        except PermissionError as exc:
            raise CollectorRunError(str(exc)) from exc
        return TenantScope(verified.tenant_id, verified.workspace_id)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS collector_runs(
              run_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, source_type TEXT NOT NULL,
              status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
              date_from TEXT NOT NULL, date_to TEXT NOT NULL, maximum_pages INTEGER NOT NULL,
              maximum_records INTEGER NOT NULL, page_delay_ms INTEGER NOT NULL,
              console_url_sha256 TEXT NOT NULL, log_page_url_sha256 TEXT NOT NULL,
              operator_confirmation INTEGER NOT NULL, login_confirmed_at TEXT,
              current_page INTEGER NOT NULL DEFAULT 0, captured_count INTEGER NOT NULL DEFAULT 0,
              accepted_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
              rejected_count INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER NOT NULL DEFAULT 0,
              last_heartbeat_at TEXT, collection_id TEXT, preview_payload_sha256 TEXT,
              preview_payload TEXT, current_safe_url TEXT, warnings TEXT NOT NULL DEFAULT '[]',
              error_code TEXT, safe_error_message TEXT, stop_requested INTEGER NOT NULL DEFAULT 0,
              worker_pid INTEGER, created_by_browser_session TEXT NOT NULL,
              runtime_setting_sha256 TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_collector_runs_environment_status
              ON collector_runs(environment_id,status,created_at);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(collector_runs)")}
            for column, value in (
                ("tenant_id", TenantScope.local_development().tenant_id),
                ("workspace_id", TenantScope.local_development().workspace_id),
                ("service_credential_id", ""),
            ):
                if column not in columns:
                    declaration = "TEXT" if column == "service_credential_id" else \
                        f"TEXT NOT NULL DEFAULT '{value}'"
                    db.execute(f"ALTER TABLE collector_runs ADD COLUMN {column} {declaration}")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_collector_runs_scope_status
              ON collector_runs(tenant_id,workspace_id,environment_id,status,created_at)""")

    def resolve_environment(self, environment_id: str) -> dict[str, Any]:
        if environment_id not in {"china_uat", "overseas"}:
            raise CollectorRunError("collector_environment_required")
        if environment_id == "overseas" and self.runtime_settings:
            item = self.runtime_settings().active("overseas", "logs_page_url")
            if not item:
                raise CollectorRunError("collector_log_page_unconfirmed")
            console_url = "https://weimeta.ai"
            log_url = item["setting_value"]
            allowed = {"weimeta.ai"}
            source = "measured_overseas_browser_collector"
            runtime_sha = item["value_sha256"]
        else:
            normalized, config, hosts, source = _collector_context(environment_id)
            console_url = str(config.get("console_base_url"))
            log_url = str(config.get("logs_page_url") or config.get("log_page_url"))
            allowed = set(hosts)
            runtime_sha = hashlib.sha256(log_url.encode()).hexdigest()
            environment_id = normalized
        for value in (console_url, log_url):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed:
                raise CollectorRunError("collector_environment_mismatch")
        return {
            "environment_id": environment_id, "console_url": console_url,
            "log_page_url": log_url, "allowed_hosts": sorted(allowed),
            "source_type": source, "runtime_setting_sha256": runtime_sha,
        }

    @staticmethod
    def validate_limits(date_from: str, date_to: str, pages: int, records: int, delay: int) -> None:
        try:
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError as exc:
            raise CollectorRunError("collector_date_invalid") from exc
        if end < start or (end - start).days > 30:
            raise CollectorRunError("collector_date_range_exceeded")
        if not 1 <= pages <= 10 or not 1 <= records <= 500 or not 1000 <= delay <= 30000:
            raise CollectorRunError("collector_limits_invalid")

    def create(self, *, environment_id: str, date_from: str, date_to: str,
               maximum_pages: int, maximum_records: int, page_delay_ms: int,
               operator_confirmation: bool, browser_session: str,
               principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.oneshot.execute")
        if not operator_confirmation:
            raise CollectorRunError("collector_operator_confirmation_required")
        self.validate_limits(date_from, date_to, maximum_pages, maximum_records, page_delay_ms)
        contract = self.resolve_environment(environment_id)
        with self.connect() as db:
            conflict = db.execute(
                f"""SELECT run_id FROM collector_runs WHERE status IN ({','.join('?' for _ in ACTIVE_STATES)})
                AND tenant_id=? AND workspace_id=?
                AND (environment_id=? OR created_by_browser_session=?) LIMIT 1""",
                (*sorted(ACTIVE_STATES), *scope.sql_parameters(), environment_id, browser_session),
            ).fetchone()
            if conflict:
                raise CollectorRunError("collector_already_running")
            run_id = f"CRUN-{uuid.uuid4().hex[:20].upper()}"
            now = _now()
            db.execute("""INSERT INTO collector_runs(
              run_id,environment_id,source_type,status,created_at,date_from,date_to,
              maximum_pages,maximum_records,page_delay_ms,console_url_sha256,log_page_url_sha256,
              operator_confirmation,created_by_browser_session,runtime_setting_sha256,
              tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                run_id, environment_id, contract["source_type"], "created", now, date_from, date_to,
                maximum_pages, maximum_records, page_delay_ms,
                hashlib.sha256(contract["console_url"].encode()).hexdigest(),
                hashlib.sha256(contract["log_page_url"].encode()).hexdigest(), 1,
                browser_session, contract["runtime_setting_sha256"],
                *scope.sql_parameters(),
            ))
        return self.get(run_id, include_private=True, principal=principal,
                        permission="collector.oneshot.execute")

    def get(self, run_id: str, include_private: bool = False, *,
            principal: PrincipalContext | None = None,
            permission: str = "collector.session.read") -> dict[str, Any]:
        scope = self._scope(principal, permission)
        with self.connect() as db:
            row = db.execute("""SELECT * FROM collector_runs WHERE run_id=?
              AND tenant_id=? AND workspace_id=?""",
              (run_id, *scope.sql_parameters())).fetchone()
        if not row:
            raise LookupError("collector_run_not_found")
        value = dict(row)
        value["warnings"] = json.loads(value.get("warnings") or "[]")
        value["operator_confirmation"] = bool(value["operator_confirmation"])
        value["stop_requested"] = bool(value["stop_requested"])
        if not include_private:
            for key in ("preview_payload", "created_by_browser_session", "runtime_setting_sha256",
                        "console_url_sha256", "log_page_url_sha256", "worker_pid"):
                value.pop(key, None)
        return value

    def list(self, environment_id: str | None = None, status: str | None = None,
             date_from: str | None = None, date_to: str | None = None, *,
             principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._scope(principal, "collector.session.read")
        clauses, args = ["tenant_id=?", "workspace_id=?"], list(scope.sql_parameters())
        if environment_id:
            if environment_id not in {"china_uat", "overseas"}:
                raise CollectorRunError("collector_environment_required")
            clauses.append("environment_id=?"); args.append(environment_id)
        if status:
            clauses.append("status=?"); args.append(status)
        if date_from:
            clauses.append("substr(created_at,1,10)>=?"); args.append(date_from)
        if date_to:
            clauses.append("substr(created_at,1,10)<=?"); args.append(date_to)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            ids = [r[0] for r in db.execute(
                f"SELECT run_id FROM collector_runs{where} ORDER BY created_at DESC LIMIT 100", args)]
        return [self.get(run_id, principal=principal) for run_id in ids]

    def transition(self, run_id: str, target: str, *,
                   principal: PrincipalContext | None = None,
                   permission: str = "collector.oneshot.execute", **updates: Any) -> dict[str, Any]:
        scope = self._scope(principal, permission)
        current = self.get(run_id, include_private=True, principal=principal,
                           permission=permission)
        if target not in TRANSITIONS.get(current["status"], set()):
            raise CollectorRunError("collector_invalid_state_transition")
        now = _now()
        updates = dict(updates)
        if target == "launching_browser": updates.setdefault("started_at", now)
        if target == "login_confirmed": updates.setdefault("login_confirmed_at", now)
        if target in TERMINAL_STATES: updates.setdefault("finished_at", now)
        allowed = {
            "started_at", "finished_at", "login_confirmed_at", "worker_pid", "error_code",
            "safe_error_message", "collection_id", "preview_payload_sha256", "preview_payload",
            "current_safe_url", "current_page", "captured_count", "accepted_count",
            "duplicate_count", "rejected_count", "elapsed_ms", "last_heartbeat_at", "warnings",
        }
        if set(updates) - allowed:
            raise CollectorRunError("collector_update_not_allowed")
        if "warnings" in updates and not isinstance(updates["warnings"], str):
            updates["warnings"] = json.dumps(updates["warnings"], ensure_ascii=False)
        assignments = ["status=?"] + [f"{key}=?" for key in updates]
        with self.connect() as db:
            changed = db.execute(f"""UPDATE collector_runs SET {','.join(assignments)}
              WHERE run_id=? AND tenant_id=? AND workspace_id=?""",
              (target, *updates.values(), run_id, *scope.sql_parameters())).rowcount
            if changed != 1:
                raise LookupError("collector_run_not_found")
        return self.get(run_id, include_private=True, principal=principal,
                        permission=permission)

    def update(self, run_id: str, *, principal: PrincipalContext | None = None,
               permission: str = "collector.oneshot.execute", **updates: Any) -> dict[str, Any]:
        scope = self._scope(principal, permission)
        current = self.get(run_id, include_private=True, principal=principal,
                           permission=permission)
        allowed = {"current_safe_url", "current_page", "captured_count", "accepted_count",
                   "duplicate_count", "rejected_count", "elapsed_ms", "last_heartbeat_at",
                   "worker_pid", "warnings"}
        if set(updates) - allowed:
            raise CollectorRunError("collector_update_not_allowed")
        if "warnings" in updates:
            updates["warnings"] = json.dumps(updates["warnings"], ensure_ascii=False)
        if updates:
            with self.connect() as db:
                db.execute("UPDATE collector_runs SET " + ",".join(f"{x}=?" for x in updates) +
                           " WHERE run_id=? AND tenant_id=? AND workspace_id=?",
                           (*updates.values(), run_id, *scope.sql_parameters()))
        return self.get(run_id, include_private=True, principal=principal,
                        permission=permission)

    def request_stop(self, run_id: str, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "collector.oneshot.execute")
        run = self.get(run_id, include_private=True, principal=principal,
                       permission="collector.oneshot.execute")
        if run["status"] in TERMINAL_STATES:
            return run
        with self.connect() as db:
            db.execute("""UPDATE collector_runs SET stop_requested=1 WHERE run_id=?
              AND tenant_id=? AND workspace_id=?""",(run_id,*scope.sql_parameters()))
        if run["status"] != "stop_requested":
            return self.transition(run_id, "stop_requested", principal=principal)
        return self.get(run_id, include_private=True, principal=principal,
                        permission="collector.oneshot.execute")

    def heartbeat_lost(self, run_id: str, maximum_age_seconds: int = 15, *,
                       principal: PrincipalContext | None = None,
                       permission: str = "evidence.import") -> bool:
        run = self.get(run_id, include_private=True, principal=principal,
                       permission=permission)
        heartbeat = run.get("last_heartbeat_at")
        if run["status"] not in ACTIVE_STATES or not heartbeat:
            return False
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat)).total_seconds()
        if age <= maximum_age_seconds:
            return False
        self.transition(run_id, "heartbeat_lost", principal=principal, permission=permission,
                        error_code="collector_heartbeat_lost",
                        safe_error_message="采集进程心跳已中断。")
        return True

    def matches_session(self, run_id: str, browser_session: str, *,
                        principal: PrincipalContext | None = None) -> bool:
        return self.get(run_id, include_private=True, principal=principal)["created_by_browser_session"] == browser_session

    def verify_runtime(self, run_id: str, *, principal: PrincipalContext | None = None,
                       permission: str = "evidence.import") -> dict[str, Any]:
        run = self.get(run_id, include_private=True, principal=principal,
                       permission=permission)
        current = self.resolve_environment(run["environment_id"])
        if current["runtime_setting_sha256"] != run["runtime_setting_sha256"]:
            raise CollectorRunError("collector_environment_mismatch")
        return current

    def save_preview(self, run_id: str, preview: dict[str, Any], *,
                     principal: PrincipalContext | None = None,
                     permission: str = "evidence.import") -> dict[str, Any]:
        payload = json.dumps(preview, ensure_ascii=False, sort_keys=True)
        return self.transition(
            run_id, "preview_ready", principal=principal, permission=permission,
            collection_id=preview["collection_id"],
            preview_payload_sha256=preview["payload_sha256"], preview_payload=payload,
            captured_count=preview.get("captured_count", 0),
            accepted_count=preview.get("record_count", 0),
            duplicate_count=preview.get("duplicate_count", 0),
            rejected_count=preview.get("rejected_count", 0),
            warnings=preview.get("warnings", []), last_heartbeat_at=_now(),
        )

    def preview(self, run_id: str, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        run = self.get(run_id, include_private=True, principal=principal)
        if run["status"] not in {"preview_ready", "import_confirmed", "completed"} or not run["preview_payload"]:
            raise CollectorRunError("collector_preview_not_ready")
        return json.loads(run["preview_payload"])
