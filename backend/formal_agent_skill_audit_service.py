"""Persistent redacted audit trail for formal Skill invocations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from formal_agent_skill_redaction import redact
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope


class FormalAgentSkillAuditService:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS formal_agent_skill_audit(
              row_id INTEGER PRIMARY KEY AUTOINCREMENT,
              audit_id TEXT NOT NULL UNIQUE,
              invocation_id TEXT NOT NULL UNIQUE,
              skill_id TEXT NOT NULL,
              decision_id TEXT NOT NULL,
              evidence_ids_json TEXT NOT NULL,
              actor_hash TEXT NOT NULL,
              origin TEXT NOT NULL,
              binding TEXT NOT NULL,
              status TEXT NOT NULL,
              request_sha256 TEXT NOT NULL,
              safe_result_json TEXT,
              redaction_count INTEGER NOT NULL DEFAULT 0,
              network_called INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              completed_at TEXT,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
            """)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _hash(value: Any) -> str:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, default=str)
        return hashlib.sha256(raw.encode("ascii")).hexdigest()

    @staticmethod
    def _scope(principal: PrincipalContext | None) -> TenantScope:
        if principal is None:
            return TenantScope.local_development()
        if not isinstance(principal, PrincipalContext) or not principal.is_verified:
            raise PermissionError("principal_context_missing_or_unverified")
        return TenantScope(principal.tenant_id, principal.workspace_id)

    def begin(self, invocation: Mapping[str, Any], claims: Mapping[str, Any], binding: str,
              *, principal: PrincipalContext | None = None) -> str:
        now = datetime.now(timezone.utc).isoformat()
        audit_id = f"FAA-{uuid.uuid4()}"
        safe_invocation_id, _ = redact(str(invocation["invocation_id"]))
        safe_decision_id, _ = redact(str(invocation["decision_id"]))
        safe_evidence, _ = redact(invocation["evidence_ids"])
        scope = self._scope(principal)
        with self.connect() as db:
            try:
                cursor = db.execute("""INSERT INTO formal_agent_skill_audit(
                  audit_id,invocation_id,skill_id,decision_id,evidence_ids_json,actor_hash,origin,
                  binding,status,request_sha256,created_at,tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    audit_id, safe_invocation_id, invocation["skill_id"], safe_decision_id,
                    json.dumps(safe_evidence, sort_keys=True),
                    hashlib.sha256(str(claims["actor_id"]).encode()).hexdigest()[:20],
                    claims["origin"], binding, "started", self._hash(invocation), now,
                    scope.tenant_id, scope.workspace_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("formal_skill_duplicate_invocation_id") from exc
            return audit_id

    def finish(self, audit_id: str, *, status: str, result: Any,
               network_called: bool = False,
               principal: PrincipalContext | None = None) -> None:
        safe, count = redact(result)
        scope = self._scope(principal)
        with self.connect() as db:
            db.execute("""UPDATE formal_agent_skill_audit SET status=?,safe_result_json=?,
              redaction_count=?,network_called=?,completed_at=? WHERE audit_id=?
              AND tenant_id=? AND workspace_id=?""", (
                status, json.dumps(safe, sort_keys=True), count, int(network_called),
                datetime.now(timezone.utc).isoformat(), audit_id,
                scope.tenant_id, scope.workspace_id))

    def deny(self, invocation: Any, error: Any) -> str:
        """Persist a safe denial even when an invocation fails before a grant."""
        value = dict(invocation) if isinstance(invocation, Mapping) else {}
        safe_error, count = redact(str(error))
        supplied, supplied_redactions = redact(
            str(value.get("invocation_id") or "unresolved")[:72])
        identifier = f"{supplied}:DENY-{uuid.uuid4()}"[:128]
        skill_id, skill_redactions = redact(
            str(value.get("skill_id") or "unresolved")[:128])
        decision_id, decision_redactions = redact(
            str(value.get("decision_id") or "unresolved")[:128])
        evidence = value.get("evidence_ids") if isinstance(value.get("evidence_ids"), list) else []
        safe_evidence, evidence_redactions = redact(evidence[:50])
        audit_id = f"FAA-{uuid.uuid4()}"
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO formal_agent_skill_audit(
              audit_id,invocation_id,skill_id,decision_id,evidence_ids_json,actor_hash,origin,
              binding,status,request_sha256,safe_result_json,redaction_count,
              network_called,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                audit_id, identifier, skill_id, decision_id, json.dumps(safe_evidence, sort_keys=True),
                hashlib.sha256(b"untrusted").hexdigest()[:20], "untrusted", "unresolved",
                "blocked", self._hash(value), json.dumps({"error": safe_error}),
                count + supplied_redactions + skill_redactions + decision_redactions
                + evidence_redactions, 0,
                datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()))
            return audit_id

    def list(self, limit: int = 100, *,
             principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError("formal_skill_audit_limit_invalid")
        scope = self._scope(principal)
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM formal_agent_skill_audit WHERE tenant_id=? AND workspace_id=? "
                "ORDER BY row_id DESC LIMIT ?",
                (scope.tenant_id, scope.workspace_id, limit))]

    def safe_summary(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Return aggregate-only audit metadata safe for a browser status page."""
        scope = self._scope(principal)
        with self.connect() as db:
            total = int(db.execute(
                "SELECT COUNT(*) FROM formal_agent_skill_audit "
                "WHERE tenant_id=? AND workspace_id=?",
                scope.sql_parameters()).fetchone()[0])
            latest = db.execute(
                "SELECT status FROM formal_agent_skill_audit WHERE tenant_id=? "
                "AND workspace_id=? ORDER BY row_id DESC LIMIT 1",
                scope.sql_parameters(),
            ).fetchone()
        status = str(latest["status"]) if latest is not None else None
        allowed = {"started", "success", "blocked", "unavailable"}
        event_type = (
            f"formal_skill_invocation_{status}"
            if status in allowed else "formal_skill_invocation_unknown"
        ) if status is not None else None
        return {
            "total_events": total,
            "latest_event_type": event_type,
        }
