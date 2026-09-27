"""Tenant-scoped, approval-gated and budgeted real-UAT execution control."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from backend.tenant_security import TenantScope


class UatExecutionControlError(ValueError):
    pass


TERMINAL_STATES = {"STOPPED", "EXPIRED", "BUDGET_EXHAUSTED", "KILLED"}


class UatExecutionControlService:
    """Persist a DRAFT→APPROVED→ACTIVE task; credentials never enter this store."""

    def __init__(self, path: Path, scope: TenantScope | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.path = path
        self.scope = scope or TenantScope.local_development()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._init()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise UatExecutionControlError("execution_clock_timezone_required")
        return value.astimezone(timezone.utc)

    def _now_text(self) -> str:
        return self._now().isoformat()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS uat_execution_control_tasks(
              task_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL, environment_id TEXT NOT NULL,
              state TEXT NOT NULL, enabled INTEGER NOT NULL,
              allowed_models_json TEXT NOT NULL, allowed_channels_json TEXT NOT NULL,
              max_requests INTEGER NOT NULL, used_requests INTEGER NOT NULL,
              max_total_cost TEXT NOT NULL, reserved_cost TEXT NOT NULL,
              used_cost TEXT NOT NULL DEFAULT '0', cost_currency TEXT NOT NULL DEFAULT 'CNY',
              max_duration_seconds INTEGER NOT NULL, max_concurrency INTEGER NOT NULL,
              max_attempts_per_request INTEGER NOT NULL DEFAULT 1,
              active_requests INTEGER NOT NULL, expires_at TEXT NOT NULL,
              approval_reference TEXT, approval_expires_at TEXT,
              kill_switch_active INTEGER NOT NULL, stopped_reason TEXT,
              created_at TEXT NOT NULL, approved_at TEXT, activated_at TEXT,
              updated_at TEXT NOT NULL, revision INTEGER NOT NULL, run_id TEXT
            );
            CREATE TABLE IF NOT EXISTS uat_execution_control_audit(
              event_id TEXT PRIMARY KEY, task_id TEXT, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS uat_execution_reservations(
              reservation_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              model_id TEXT NOT NULL, channel_id TEXT NOT NULL,
              estimated_cost TEXT NOT NULL, actual_cost TEXT,
              state TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
            );
            """)
            columns = {row[1] for row in db.execute(
                "PRAGMA table_info(uat_execution_control_tasks)")}
            additions = {
                "used_cost": "TEXT NOT NULL DEFAULT '0'",
                "cost_currency": "TEXT NOT NULL DEFAULT 'CNY'",
                "approval_expires_at": "TEXT", "approved_at": "TEXT",
                "activated_at": "TEXT", "run_id": "TEXT",
                "max_attempts_per_request": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE uat_execution_control_tasks ADD COLUMN {name} {declaration}")
            db.execute("UPDATE uat_execution_control_tasks SET state=UPPER(state)")
            db.execute("DROP INDEX IF EXISTS uq_uat_execution_active_scope")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_uat_execution_active_scope
              ON uat_execution_control_tasks(tenant_id,workspace_id,environment_id)
              WHERE state='ACTIVE'""")

    def _audit(self, db, task_id: str | None, event: str,
               details: dict[str, Any]) -> None:
        db.execute("INSERT INTO uat_execution_control_audit VALUES(?,?,?,?,?,?,?)", (
            f"UCEA-{uuid.uuid4().hex.upper()}", task_id,
            *self.scope.sql_parameters(), event, self._now_text(),
            json.dumps(details, sort_keys=True, ensure_ascii=False)))

    @staticmethod
    def _identifiers(values: Any, name: str, *, required: bool = True) -> list[str]:
        if not isinstance(values, list) or (required and not values) or len(values) > 24:
            raise UatExecutionControlError(f"{name}_invalid")
        result: list[str] = []
        for raw in values:
            value = str(raw or "").strip()
            if not value or len(value) > 128 or value in result:
                raise UatExecutionControlError(f"{name}_invalid")
            result.append(value)
        return result

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        if body.get("environment_id") != "china_uat":
            raise UatExecutionControlError("execution_environment_not_allowed")
        models = self._identifiers(body.get("allowed_models"), "allowed_models")
        channels = self._identifiers(body.get("allowed_channels"), "allowed_channels")
        try:
            requests = int(body.get("max_requests"))
            cost = Decimal(str(body.get("max_total_cost")))
            duration = int(body.get("max_duration_seconds"))
            concurrency = int(body.get("max_concurrency"))
            max_attempts = int(body.get("max_attempts_per_request"))
            proposed_expiry = datetime.fromisoformat(str(body.get("expires_at")))
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise UatExecutionControlError("execution_limits_invalid") from exc
        if proposed_expiry.tzinfo is None:
            raise UatExecutionControlError("execution_expiry_timezone_required")
        now = self._now()
        proposed_expiry = proposed_expiry.astimezone(timezone.utc)
        currency = str(body.get("cost_currency") or "CNY").strip().upper()
        if currency != "CNY":
            raise UatExecutionControlError("execution_cost_currency_not_supported")
        if not (1 <= requests <= 24 and Decimal("0") < cost <= Decimal("3")
                and 60 <= duration <= 1200 and 1 <= concurrency <= 1
                and 1 <= max_attempts <= 3
                and now < proposed_expiry <= now + timedelta(seconds=duration)):
            raise UatExecutionControlError("execution_limits_invalid")
        if body.get("explicit_confirmation") is not True:
            raise UatExecutionControlError("execution_draft_confirmation_required")
        task_id = f"UATCTRL-{uuid.uuid4().hex[:20].upper()}"
        run_id = str(body.get("run_id") or f"UATRUN-{uuid.uuid4().hex[:16].upper()}")
        created = self._now_text()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("""SELECT task_id FROM uat_execution_control_tasks
              WHERE tenant_id=? AND workspace_id=? AND environment_id='china_uat'
                AND state='ACTIVE'""", self.scope.sql_parameters()).fetchone()
            if active:
                raise UatExecutionControlError("active_execution_task_already_exists")
            db.execute("""INSERT INTO uat_execution_control_tasks(
              task_id,tenant_id,workspace_id,environment_id,state,enabled,
              allowed_models_json,allowed_channels_json,max_requests,used_requests,
              max_total_cost,reserved_cost,used_cost,cost_currency,max_duration_seconds,
              max_concurrency,max_attempts_per_request,active_requests,expires_at,approval_reference,
              approval_expires_at,kill_switch_active,stopped_reason,created_at,
              approved_at,activated_at,updated_at,revision,run_id)
              VALUES(?,?,?,'china_uat','DRAFT',0,?,?,?,0,?,'0','0',?,?,?,?,0,?,NULL,
                     NULL,0,NULL,?,NULL,NULL,?,1,?)""", (
              task_id, *self.scope.sql_parameters(), json.dumps(models),
              json.dumps(channels), requests, str(cost), currency, duration,
              concurrency, max_attempts, proposed_expiry.isoformat(), created, created, run_id))
            self._audit(db, task_id, "execution_control_draft_created", {
                "run_id": run_id, "allowed_models": models,
                "allowed_channels": channels, "max_requests": requests,
                "max_total_cost": str(cost), "max_concurrency": concurrency,
                "max_attempts_per_request": max_attempts,
                "max_duration_seconds": duration,
                "expires_at": proposed_expiry.isoformat()})
            db.commit()
        return self.status(task_id)

    def approve(self, task_id: str, *, approval_reference: str,
                approval_expires_at: str) -> dict[str, Any]:
        reference = approval_reference.strip()
        if not reference or len(reference) > 256:
            raise UatExecutionControlError("execution_approval_required")
        try:
            expiry = datetime.fromisoformat(approval_expires_at)
        except ValueError as exc:
            raise UatExecutionControlError("approval_expiry_invalid") from exc
        if expiry.tzinfo is None or expiry.astimezone(timezone.utc) <= self._now():
            raise UatExecutionControlError("approval_expiry_invalid")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            if not row or row["state"] != "DRAFT":
                raise UatExecutionControlError("execution_task_not_approvable")
            effective_expiry = min(expiry.astimezone(timezone.utc),
                                   datetime.fromisoformat(row["expires_at"]))
            db.execute("""UPDATE uat_execution_control_tasks SET state='APPROVED',
              approval_reference=?,approval_expires_at=?,approved_at=?,updated_at=?,
              revision=revision+1 WHERE task_id=? AND tenant_id=? AND workspace_id=?""", (
              reference, effective_expiry.isoformat(), self._now_text(), self._now_text(),
              task_id, *self.scope.sql_parameters()))
            self._audit(db, task_id, "execution_control_approved", {
                "approval_reference": reference,
                "approval_expires_at": effective_expiry.isoformat()})
            db.commit()
        return self.status(task_id)

    def activate(self, task_id: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            if not row or row["state"] != "APPROVED":
                raise UatExecutionControlError("execution_task_not_activatable")
            if (not row["approval_expires_at"] or
                    datetime.fromisoformat(row["approval_expires_at"]) <= self._now()):
                self._transition(db, row, "EXPIRED", "approval_expired")
                db.commit()
                raise UatExecutionControlError("execution_approval_expired")
            db.execute("""UPDATE uat_execution_control_tasks SET state='STOPPED',enabled=0,
              stopped_reason='superseded',updated_at=?,revision=revision+1
              WHERE tenant_id=? AND workspace_id=? AND environment_id='china_uat'
                AND state='ACTIVE'""", (self._now_text(), *self.scope.sql_parameters()))
            active_expiry = min(datetime.fromisoformat(row["expires_at"]),
                self._now() + timedelta(seconds=row["max_duration_seconds"]))
            db.execute("""UPDATE uat_execution_control_tasks SET state='ACTIVE',enabled=1,
              activated_at=?,expires_at=?,updated_at=?,revision=revision+1
              WHERE task_id=? AND tenant_id=? AND workspace_id=?""", (
              self._now_text(), active_expiry.isoformat(), self._now_text(), task_id,
              *self.scope.sql_parameters()))
            self._audit(db, task_id, "execution_control_activated",
                        {"expires_at": active_expiry.isoformat()})
            db.commit()
        return self.status(task_id)

    def _row(self, db, task_id: str | None = None):
        if task_id:
            return db.execute("""SELECT * FROM uat_execution_control_tasks
              WHERE tenant_id=? AND workspace_id=? AND task_id=?""",
              (*self.scope.sql_parameters(), task_id)).fetchone()
        return db.execute("""SELECT * FROM uat_execution_control_tasks
          WHERE tenant_id=? AND workspace_id=? AND environment_id='china_uat'
          ORDER BY created_at DESC LIMIT 1""", self.scope.sql_parameters()).fetchone()

    def _transition(self, db, row, state: str, reason: str) -> None:
        if row["state"] in TERMINAL_STATES:
            return
        db.execute("""UPDATE uat_execution_control_tasks SET state=?,enabled=0,
          stopped_reason=?,updated_at=?,revision=revision+1
          WHERE task_id=? AND tenant_id=? AND workspace_id=?""", (
          state, reason, self._now_text(), row["task_id"], *self.scope.sql_parameters()))
        self._audit(db, row["task_id"], "execution_control_terminal", {
            "state": state, "reason": reason})

    def _refresh(self, db, row):
        if not row or row["state"] not in {"APPROVED", "ACTIVE"}:
            return row
        now = self._now()
        if row["approval_expires_at"] and datetime.fromisoformat(row["approval_expires_at"]) <= now:
            self._transition(db, row, "EXPIRED", "approval_expired")
        elif datetime.fromisoformat(row["expires_at"]) <= now:
            self._transition(db, row, "EXPIRED", "task_expired")
        elif row["kill_switch_active"]:
            self._transition(db, row, "KILLED", "kill_switch")
        elif row["state"] == "ACTIVE" and row["active_requests"] == 0:
            if Decimal(row["used_cost"]) >= Decimal(row["max_total_cost"]):
                self._transition(db, row, "BUDGET_EXHAUSTED", "cost_budget_exhausted")
            elif row["used_requests"] >= row["max_requests"]:
                self._transition(db, row, "STOPPED", "request_budget_exhausted")
        return self._row(db, row["task_id"])

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._refresh(db, self._row(db, task_id))
            db.commit()
        if not row:
            return {"status": "DISABLED", "execution_ready": False, "task": None}
        result = dict(row)
        for key in ("allowed_models_json", "allowed_channels_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        for key in ("enabled", "kill_switch_active"):
            result[key] = bool(result[key])
        result["remaining_requests"] = max(result["max_requests"] - result["used_requests"], 0)
        result["remaining_cost"] = str(max(
            Decimal(result["max_total_cost"]) - Decimal(result["used_cost"])
            - Decimal(result["reserved_cost"]), Decimal("0")))
        with self.connect() as db:
            audit = [dict(item) for item in db.execute("""SELECT event_id,event_type,
              created_at,details_json FROM uat_execution_control_audit
              WHERE task_id=? AND tenant_id=? AND workspace_id=?
              ORDER BY created_at DESC LIMIT 50""", (
              result["task_id"], *self.scope.sql_parameters())).fetchall()]
        for item in audit:
            item["details"] = json.loads(item.pop("details_json"))
        result["audit_log"] = audit
        return {"status": result["state"],
                "execution_ready": result["state"] == "ACTIVE" and result["enabled"],
                "task": result}

    def reserve(self, model: str, channel: str, estimated_cost: Decimal) -> dict[str, str]:
        if estimated_cost < 0:
            raise UatExecutionControlError("invalid_estimated_cost")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._refresh(db, self._row(db))
            if not row or row["state"] != "ACTIVE" or not row["enabled"]:
                raise UatExecutionControlError("controlled_execution_not_active")
            if model not in json.loads(row["allowed_models_json"]):
                raise UatExecutionControlError("model_not_approved_by_execution_task")
            if channel not in json.loads(row["allowed_channels_json"]):
                raise UatExecutionControlError("channel_not_approved_by_execution_task")
            if row["active_requests"] >= row["max_concurrency"]:
                raise UatExecutionControlError("execution_concurrency_limit_reached")
            if row["used_requests"] >= row["max_requests"]:
                raise UatExecutionControlError("execution_request_limit_reached")
            total = Decimal(row["used_cost"]) + Decimal(row["reserved_cost"]) + estimated_cost
            if total > Decimal(row["max_total_cost"]):
                raise UatExecutionControlError("execution_budget_exceeded")
            reservation_id = f"UATRES-{uuid.uuid4().hex[:20].upper()}"
            cursor = db.execute("""UPDATE uat_execution_control_tasks SET
              used_requests=used_requests+1,active_requests=active_requests+1,
              reserved_cost=CAST(reserved_cost AS NUMERIC)+?,updated_at=?,revision=revision+1
              WHERE task_id=? AND revision=? AND state='ACTIVE'""", (
              str(estimated_cost), self._now_text(), row["task_id"], row["revision"]))
            if cursor.rowcount != 1:
                raise UatExecutionControlError("execution_reservation_race_lost")
            db.execute("INSERT INTO uat_execution_reservations VALUES(?,?,?,?,?,?,?,?,?,?,NULL)", (
              reservation_id, row["task_id"], *self.scope.sql_parameters(), model,
              channel, str(estimated_cost), None, "ACTIVE", self._now_text()))
            self._audit(db, row["task_id"], "execution_capacity_reserved", {
                "reservation_id": reservation_id, "model": model,
                "channel": channel, "estimated_cost": str(estimated_cost)})
            db.commit()
        return {"task_id": row["task_id"], "reservation_id": reservation_id}

    def complete(self, reservation_id: str, outcome: str,
                 actual_cost: Decimal | None = None) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reservation = db.execute("""SELECT * FROM uat_execution_reservations
              WHERE reservation_id=? AND tenant_id=? AND workspace_id=?""", (
              reservation_id, *self.scope.sql_parameters())).fetchone()
            if not reservation or reservation["state"] != "ACTIVE":
                raise UatExecutionControlError("execution_reservation_not_active")
            charged = actual_cost if actual_cost is not None else Decimal(reservation["estimated_cost"])
            if charged < 0:
                raise UatExecutionControlError("invalid_actual_cost")
            db.execute("""UPDATE uat_execution_control_tasks SET
              active_requests=CASE WHEN active_requests>0 THEN active_requests-1 ELSE 0 END,
              reserved_cost=MAX(CAST(reserved_cost AS NUMERIC)-?,0),
              used_cost=CAST(used_cost AS NUMERIC)+?,updated_at=?,revision=revision+1
              WHERE task_id=? AND tenant_id=? AND workspace_id=?""", (
              reservation["estimated_cost"], str(charged), self._now_text(),
              reservation["task_id"], *self.scope.sql_parameters()))
            db.execute("""UPDATE uat_execution_reservations SET state='COMPLETED',
              actual_cost=?,completed_at=? WHERE reservation_id=?""", (
              str(charged), self._now_text(), reservation_id))
            self._audit(db, reservation["task_id"], "execution_attempt_completed", {
                "reservation_id": reservation_id, "outcome": outcome,
                "actual_cost": str(charged),
                "actual_cost_source": "observed" if actual_cost is not None else "estimated_fallback"})
            row = self._row(db, reservation["task_id"])
            self._refresh(db, row)
            db.commit()

    def stop(self, reason: str, *, kill_switch: bool = False) -> dict[str, Any]:
        if not reason or len(reason) > 256:
            raise UatExecutionControlError("stop_reason_required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db)
            if row and row["state"] not in TERMINAL_STATES:
                if kill_switch:
                    db.execute("""UPDATE uat_execution_control_tasks SET kill_switch_active=1
                      WHERE task_id=? AND tenant_id=? AND workspace_id=?""", (
                      row["task_id"], *self.scope.sql_parameters()))
                self._transition(db, self._row(db, row["task_id"]),
                                 "KILLED" if kill_switch else "STOPPED", reason)
            db.commit()
        return self.status(row["task_id"] if row else None)
