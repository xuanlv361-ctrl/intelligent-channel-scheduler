"""Persistent SQLite service for policy-aware sticky routing.

All operations are local.  The schema stores only keyed fingerprints and
bounded structured evidence identifiers, never affinity inputs or request
content.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType
from backend.tenant_security import TenantScope

from sticky_routing import (
    GATE_ORDER, StickyRoutingError, derive_route_scope, load_policy,
    normalize_gate_results,
)


TERMINAL_STATES = frozenset({"EXPIRED", "INVALIDATED", "INTERRUPTED"})
ALL_STATES = frozenset({"ACTIVE", *TERMINAL_STATES})
SCHEMA_VERSION = 5
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _safe_identifier(value: Any, field: str, *, required: bool = True) -> str | None:
    text = str(value or "").strip()
    if not text and not required:
        return None
    if not SAFE_IDENTIFIER.fullmatch(text):
        raise StickyRoutingError(f"unsafe_{field}")
    return text


class StickyRoutingService:
    def __init__(
        self,
        database_path: str | Path,
        policy_path: str | Path,
        *,
        secret: str | None = None,
        key_id: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
        authorization_service: AuthorizationService | None = None,
    ):
        self.path = Path(database_path)
        self.policy_path = Path(policy_path)
        self.policy = load_policy(policy_path)
        self.secret = secret if secret is not None else os.getenv(
            self.policy.get("route_key_secret_env", "STICKY_ROUTE_KEY_SECRET"), "")
        self.key_id = key_id if key_id is not None else os.getenv(
            self.policy.get("route_key_id_env", "STICKY_ROUTE_KEY_ID"), "")
        self.clock = clock
        self.authorization_service = authorization_service
        self._eligibility_provider_configured = False
        self._migration_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self.recover()

    def _tenant_scope(self, principal: PrincipalContext | None, permission: str,
                      *, scheduler_only: bool = False) -> TenantScope:
        if self.authorization_service is None:
            return TenantScope.local_development()
        verified = self.authorization_service.authorize(principal, permission)
        if scheduler_only and (verified.principal_type is not PrincipalType.SERVICE
                               or "scheduler_service" not in verified.roles):
            raise PermissionError("scheduler_service_identity_required")
        return TenantScope(verified.tenant_id, verified.workspace_id)

    @staticmethod
    def _scope_token(scope: TenantScope) -> str:
        import hashlib
        return hashlib.sha256(
            f"{scope.tenant_id}\x00{scope.workspace_id}".encode()).hexdigest()[:20]

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def _connection(self):
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _migrate(self) -> None:
        with self._migration_lock, self._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS sticky_schema_migrations(
              version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sticky_bindings(
              sticky_binding_id TEXT PRIMARY KEY,
              route_key_fingerprint TEXT NOT NULL,
              affinity_fingerprint TEXT,
              safe_route_key_fingerprint TEXT NOT NULL,
              route_key_id TEXT NOT NULL,
              route_key_derivation_version TEXT NOT NULL,
              capability_scope_version TEXT,
              environment_id TEXT NOT NULL,
              requested_model TEXT NOT NULL,
              capability_scope_json TEXT NOT NULL,
              capability_scope_hash TEXT NOT NULL,
              selected_channel TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('ACTIVE','EXPIRED','INVALIDATED','INTERRUPTED')),
              policy_version TEXT NOT NULL,
              configuration_version TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              maximum_expires_at TEXT NOT NULL,
              last_used_at TEXT NOT NULL,
              hit_count INTEGER NOT NULL DEFAULT 0 CHECK(hit_count >= 0),
              invalidation_reason TEXT,
              interruption_reason TEXT,
              originating_decision_id TEXT NOT NULL,
              latest_decision_id TEXT NOT NULL,
              metric_snapshot_id TEXT,
              confidence_snapshot_id TEXT,
              evidence_ids_json TEXT NOT NULL DEFAULT '[]',
              is_mock INTEGER NOT NULL DEFAULT 1 CHECK(is_mock IN (0,1)),
              revision INTEGER NOT NULL DEFAULT 0,
              audit_timestamp TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_sticky_binding_filters
              ON sticky_bindings(environment_id,state,requested_model,selected_channel,created_at DESC);
            CREATE TABLE IF NOT EXISTS sticky_routing_events(
              event_id TEXT PRIMARY KEY,
              sticky_binding_id TEXT,
              decision_id TEXT,
              outcome TEXT NOT NULL,
              reason TEXT,
              environment_id TEXT NOT NULL,
              requested_model TEXT NOT NULL,
              selected_channel TEXT,
              policy_version TEXT NOT NULL,
              configuration_version TEXT NOT NULL,
              metric_snapshot_id TEXT,
              confidence_snapshot_id TEXT,
              evidence_ids_json TEXT NOT NULL DEFAULT '[]',
              gate_trace_json TEXT NOT NULL DEFAULT '[]',
              safe_route_key_fingerprint TEXT,
              state_before TEXT,
              state_after TEXT,
              expires_at TEXT,
              maximum_expires_at TEXT,
              last_used_at TEXT,
              hit_count INTEGER,
              is_mock INTEGER NOT NULL DEFAULT 1,
              audit_timestamp TEXT NOT NULL,
              FOREIGN KEY(sticky_binding_id) REFERENCES sticky_bindings(sticky_binding_id)
            );
            CREATE INDEX IF NOT EXISTS ix_sticky_events_time
              ON sticky_routing_events(audit_timestamp DESC);
            CREATE TABLE IF NOT EXISTS sticky_mutation_idempotency(
              idempotency_key TEXT PRIMARY KEY,
              request_sha256 TEXT NOT NULL,
              response_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sticky_decision_idempotency(
              decision_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              scope_sha256 TEXT NOT NULL,
              response_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(decision_id,operation)
            );
            CREATE TABLE IF NOT EXISTS sticky_miss_authorizations(
              decision_id TEXT PRIMARY KEY,
              route_key_fingerprint TEXT NOT NULL,
              eligible_channel_ids_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              consumed_at TEXT
            );
            """)
            db.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(sticky_bindings)")
            }
            if "affinity_fingerprint" not in columns:
                db.execute("ALTER TABLE sticky_bindings ADD COLUMN affinity_fingerprint TEXT")
            if "capability_scope_version" not in columns:
                db.execute("ALTER TABLE sticky_bindings ADD COLUMN capability_scope_version TEXT")
            event_columns = {
                row["name"] for row in db.execute(
                    "PRAGMA table_info(sticky_routing_events)")
            }
            for column, sql_type in (
                ("safe_route_key_fingerprint", "TEXT"),
                ("state_before", "TEXT"),
                ("state_after", "TEXT"),
                ("expires_at", "TEXT"),
                ("maximum_expires_at", "TEXT"),
                ("last_used_at", "TEXT"),
                ("hit_count", "INTEGER"),
            ):
                if column not in event_columns:
                    db.execute(
                        f"ALTER TABLE sticky_routing_events ADD COLUMN {column} {sql_type}")
            duplicate_routes = db.execute("""SELECT route_key_fingerprint
              FROM sticky_bindings WHERE state='ACTIVE'
              GROUP BY route_key_fingerprint HAVING COUNT(*)>1""").fetchall()
            for duplicate in duplicate_routes:
                rows = db.execute("""SELECT sticky_binding_id FROM sticky_bindings
                  WHERE route_key_fingerprint=? AND state='ACTIVE'
                  ORDER BY created_at ASC,sticky_binding_id ASC""",
                  (duplicate["route_key_fingerprint"],)).fetchall()
                for row in rows[1:]:
                    db.execute("""UPDATE sticky_bindings SET state='INVALIDATED',
                      invalidation_reason='migration_duplicate_active_binding',
                      audit_timestamp=?,revision=revision+1
                      WHERE sticky_binding_id=? AND state='ACTIVE'""",
                      (_iso(self.clock()), row["sticky_binding_id"]))
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_sticky_active_route
              ON sticky_bindings(route_key_fingerprint) WHERE state='ACTIVE'""")
            db.execute("DROP INDEX IF EXISTS uq_sticky_decision_outcome")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_sticky_event_identity
              ON sticky_routing_events(
                decision_id,outcome,IFNULL(sticky_binding_id,'-'))
              WHERE decision_id IS NOT NULL""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_sticky_active_affinity
              ON sticky_bindings(affinity_fingerprint,state)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_sticky_active_expiry
              ON sticky_bindings(state,expires_at,maximum_expires_at)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_sticky_model_created
              ON sticky_bindings(requested_model,created_at DESC)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_sticky_channel_created
              ON sticky_bindings(selected_channel,created_at DESC)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_sticky_event_metrics
              ON sticky_routing_events(environment_id,outcome,reason)""")
            for version in range(1, SCHEMA_VERSION + 1):
                db.execute(
                    "INSERT OR IGNORE INTO sticky_schema_migrations(version,applied_at) VALUES(?,?)",
                    (version, _iso(self.clock())),
                )
            db.commit()

    @property
    def operational(self) -> bool:
        return bool(self.policy["enabled"] and self.secret and self.key_id)

    def set_eligibility_provider_configured(self, configured: bool) -> None:
        self._eligibility_provider_configured = bool(configured)

    def _scope(self, **kwargs: Any):
        return derive_route_scope(
            secret=self.secret, key_id=self.key_id,
            policy_version=self.policy["policy_version"],
            configuration_version=self.policy["configuration_version"],
            derivation_version=self.policy["route_key_derivation_version"],
            capability_scope_version=self.policy["capability_scope_version"],
            **kwargs,
        )

    def _evidence(self, evidence: Mapping[str, Any] | None) -> tuple[str | None, str | None, list[str]]:
        value = dict(evidence or {})
        maximum = int(self.policy["maximum_evidence_ids"])
        ids = []
        for item in value.get("evidence_ids", []):
            text = _safe_identifier(item, "evidence_id")
            if text and text not in ids:
                ids.append(text)
            if len(ids) >= maximum:
                break
        return (
            _safe_identifier(value.get("metric_snapshot_id"), "metric_snapshot_id", required=False),
            _safe_identifier(value.get("confidence_snapshot_id"), "confidence_snapshot_id", required=False),
            ids,
        )

    def _decision_hash(self, value: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        import hashlib
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def _decision_replay(
        self, db: sqlite3.Connection, decision_id: str, operation: str,
        scope_sha256: str,
    ) -> dict[str, Any] | None:
        row = db.execute("""SELECT * FROM sticky_decision_idempotency
          WHERE decision_id=? AND operation=?""", (decision_id, operation)).fetchone()
        if not row:
            return None
        if row["scope_sha256"] != scope_sha256:
            raise StickyRoutingError("decision_id_scope_conflict")
        response = json.loads(row["response_json"])
        response["idempotent_replay"] = True
        return response

    def _decision_store(
        self, db: sqlite3.Connection, decision_id: str, operation: str,
        scope_sha256: str, response: Mapping[str, Any],
    ) -> None:
        db.execute("""INSERT INTO sticky_decision_idempotency(
          decision_id,operation,scope_sha256,response_json,created_at)
          VALUES(?,?,?,?,?)""", (
            decision_id, operation, scope_sha256,
            json.dumps(dict(response), ensure_ascii=False, separators=(",", ":")),
            _iso(self.clock()),
        ))

    def _safe_gate_trace(self, trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
        safe = []
        for item in trace:
            gate = _safe_identifier(item.get("gate"), "gate")
            state = _safe_identifier(item.get("state"), "gate_state")
            row = {"gate": gate, "state": state, "passed": bool(item.get("passed"))}
            reason = _safe_identifier(
                item.get("reason"), "gate_reason", required=False)
            evidence_id = _safe_identifier(
                item.get("evidence_id"), "gate_evidence_id", required=False)
            if reason:
                row["reason"] = reason
            if evidence_id:
                row["evidence_id"] = evidence_id
            safe.append(row)
        return safe

    def _event(
        self, db: sqlite3.Connection, *, outcome: str, decision_id: str | None,
        binding_id: str | None, environment_id: str, requested_model: str,
        selected_channel: str | None, reason: str | None, gate_trace: list[dict[str, Any]],
        evidence: Mapping[str, Any] | None, is_mock: bool,
    ) -> None:
        metric_id, confidence_id, ids = self._evidence(evidence)
        decision_id = _safe_identifier(
            decision_id, "decision_id", required=False)
        environment_id = _safe_identifier(environment_id, "environment_id")
        requested_model = _safe_identifier(requested_model, "requested_model")
        selected_channel = _safe_identifier(
            selected_channel, "selected_channel", required=False)
        reason = _safe_identifier(reason, "reason", required=False)
        gate_trace = self._safe_gate_trace(gate_trace)
        binding = None
        if binding_id:
            binding = db.execute(
                "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?",
                (binding_id,),
            ).fetchone()
        state_after = binding["state"] if binding else None
        state_before = (
            None if outcome == "CREATED"
            else "ACTIVE" if outcome in {"HIT", *TERMINAL_STATES}
            else state_after
        )
        db.execute("""INSERT OR IGNORE INTO sticky_routing_events(
          event_id,sticky_binding_id,decision_id,outcome,reason,environment_id,
          requested_model,selected_channel,policy_version,configuration_version,
          metric_snapshot_id,confidence_snapshot_id,evidence_ids_json,
          gate_trace_json,safe_route_key_fingerprint,state_before,state_after,
          expires_at,maximum_expires_at,last_used_at,hit_count,is_mock,audit_timestamp)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            f"STEV-{uuid.uuid4().hex}", binding_id, decision_id, outcome, reason,
            environment_id, requested_model, selected_channel,
            self.policy["policy_version"], self.policy["configuration_version"],
            metric_id, confidence_id, json.dumps(ids, separators=(",", ":")),
            json.dumps(gate_trace, separators=(",", ":")),
            binding["safe_route_key_fingerprint"] if binding else None,
            state_before, state_after,
            binding["expires_at"] if binding else None,
            binding["maximum_expires_at"] if binding else None,
            binding["last_used_at"] if binding else None,
            binding["hit_count"] if binding else None,
            int(is_mock),
            _iso(self.clock()),
        ))

    def _public(self, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.pop("route_key_fingerprint", None)
        item.pop("affinity_fingerprint", None)
        item["capability_scope"] = json.loads(item.pop("capability_scope_json"))
        item["evidence_ids"] = json.loads(item.pop("evidence_ids_json"))
        item["is_mock"] = bool(item["is_mock"])
        item["evidence_source"] = (
            "local_mock_evidence" if item["is_mock"]
            else "structured_scheduler_evidence")
        now = self.clock()
        item["persisted_state"] = item["state"]
        elapsed = (
            item["state"] == "ACTIVE"
            and (
                _parse(item["expires_at"]) <= now
                or _parse(item["maximum_expires_at"]) <= now
            )
        )
        if elapsed:
            item["state"] = "EXPIRED"
        item["expiration_pending_reconciliation"] = elapsed
        item["remaining_seconds"] = max(0, int((_parse(item["expires_at"]) - now).total_seconds()))
        return item

    def recover(self) -> int:
        # A disabled policy or temporarily unavailable key must never be
        # interpreted as a confirmed destructive rotation. Lazy reconciliation
        # resumes after the subsystem is explicitly operational again.
        if not self.policy["enabled"] or not self.secret or not self.key_id:
            return 0
        now = _iso(self.clock())
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM sticky_bindings WHERE state='ACTIVE'").fetchall()
            changed = 0
            for row in rows:
                reason = None
                state = None
                if _parse(row["expires_at"]) <= self.clock() or _parse(row["maximum_expires_at"]) <= self.clock():
                    state, reason = "EXPIRED", "ttl_or_maximum_duration_elapsed"
                elif (
            row["policy_version"] != self.policy["policy_version"]
                    or row["configuration_version"] != self.policy["configuration_version"]
                    or row["route_key_derivation_version"] != self.policy["route_key_derivation_version"]
                    or row["capability_scope_version"] != self.policy["capability_scope_version"]
                    or (
                        bool(self.secret and self.key_id)
                        and row["route_key_id"] != self.key_id
                    )
                ):
                    state, reason = "INVALIDATED", "policy_configuration_or_key_rotation"
                if state:
                    db.execute("""UPDATE sticky_bindings SET state=?,invalidation_reason=?,
                      audit_timestamp=?,revision=revision+1 WHERE sticky_binding_id=? AND state='ACTIVE'""",
                      (state, reason if state == "INVALIDATED" else None, now, row["sticky_binding_id"]))
                    self._event(
                        db, outcome=state, decision_id=None, binding_id=row["sticky_binding_id"],
                        environment_id=row["environment_id"], requested_model=row["requested_model"],
                        selected_channel=row["selected_channel"], reason=reason, gate_trace=[],
                        evidence=None, is_mock=bool(row["is_mock"]),
                    )
                    changed += 1
            db.commit()
            return changed

    def resolve(
        self, *, decision_id: str, affinity: str | None, environment_id: str,
        requested_model: str, capability_scope: Mapping[str, Any] | None,
        stream: bool, mode: str, gate_results: Mapping[str, Any] | None,
        eligible_channel_ids: list[str] | tuple[str, ...] | set[str],
        evidence: Mapping[str, Any] | None = None, is_mock: bool = True,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        tenant_scope = self._tenant_scope(
            principal, "scheduler.decide", scheduler_only=True)
        decision_id = _safe_identifier(decision_id, "decision_id")
        environment_id = _safe_identifier(environment_id, "environment_id")
        requested_model = _safe_identifier(requested_model, "requested_model")
        if not self.policy["enabled"]:
            return {"outcome": "NOT_APPLICABLE", "reason": "sticky_policy_disabled", "gate_order": list(GATE_ORDER)}
        if mode not in set(self.policy.get("allowed_modes", [])):
            return {
                "outcome": "BLOCKED", "reason": "sticky_mode_not_allowed",
                "routing_blocked": True, "gate_order": list(GATE_ORDER)}
        if not self.secret or not self.key_id:
            return {"outcome": "BLOCKED", "reason": "sticky_key_material_unavailable", "gate_order": list(GATE_ORDER)}
        if not affinity:
            return {"outcome": "NOT_APPLICABLE", "reason": "sticky_affinity_not_supplied", "gate_order": list(GATE_ORDER)}
        if self.authorization_service is not None:
            affinity = f"{self._scope_token(tenant_scope)}:{affinity}"
            decision_id = f"{self._scope_token(tenant_scope)}:{decision_id}"
        scope = self._scope(
            affinity=affinity, environment_id=environment_id,
            requested_model=requested_model, capability_scope=capability_scope, stream=stream,
        )
        gate_trace, failure = normalize_gate_results(gate_results)
        gate_trace = self._safe_gate_trace(gate_trace)
        eligible = {str(value) for value in eligible_channel_ids}
        for candidate in eligible:
            _safe_identifier(candidate, "selected_channel")
        resolve_hash = self._decision_hash({
            "route": scope.route_key_fingerprint,
            "gates": gate_trace,
            "eligible": sorted(eligible),
        })
        now = self.clock()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._decision_replay(
                db, decision_id, "resolve", resolve_hash)
            if replay is not None:
                db.commit()
                return replay
            related = db.execute(
                """SELECT * FROM sticky_bindings
                   WHERE affinity_fingerprint=? AND state='ACTIVE'
                     AND route_key_fingerprint<>?""",
                (scope.affinity_fingerprint, scope.route_key_fingerprint),
            ).fetchall()
            for previous in (related if not failure else []):
                if previous["environment_id"] != environment_id:
                    change_reason = "environment_changed"
                elif previous["requested_model"] != requested_model:
                    change_reason = "requested_model_changed_incompatibly"
                else:
                    change_reason = "capability_requirements_changed"
                db.execute("""UPDATE sticky_bindings SET state='INTERRUPTED',
                  interruption_reason=?,latest_decision_id=?,audit_timestamp=?,
                  revision=revision+1 WHERE sticky_binding_id=? AND state='ACTIVE'""",
                  (change_reason, decision_id, _iso(now), previous["sticky_binding_id"]))
                self._event(
                    db, outcome="INTERRUPTED", decision_id=decision_id,
                    binding_id=previous["sticky_binding_id"],
                    environment_id=previous["environment_id"],
                    requested_model=previous["requested_model"],
                    selected_channel=previous["selected_channel"],
                    reason=change_reason, gate_trace=gate_trace,
                    evidence=evidence, is_mock=bool(previous["is_mock"]),
                )
            row = db.execute(
                "SELECT * FROM sticky_bindings WHERE route_key_fingerprint=? AND state='ACTIVE'",
                (scope.route_key_fingerprint,),
            ).fetchone()
            if row and (
                _parse(row["expires_at"]) <= now or _parse(row["maximum_expires_at"]) <= now
            ):
                db.execute("""UPDATE sticky_bindings SET state='EXPIRED',audit_timestamp=?,
                  revision=revision+1 WHERE sticky_binding_id=? AND state='ACTIVE'""",
                  (_iso(now), row["sticky_binding_id"]))
                self._event(
                    db, outcome="EXPIRED", decision_id=decision_id,
                    binding_id=row["sticky_binding_id"], environment_id=environment_id,
                    requested_model=requested_model, selected_channel=row["selected_channel"],
                    reason="ttl_or_maximum_duration_elapsed", gate_trace=gate_trace,
                    evidence=evidence, is_mock=is_mock,
                )
                row = None
            if failure:
                if row:
                    db.execute("""UPDATE sticky_bindings SET state='INTERRUPTED',
                      interruption_reason=?,latest_decision_id=?,audit_timestamp=?,
                      revision=revision+1 WHERE sticky_binding_id=? AND state='ACTIVE'""",
                      (failure, decision_id, _iso(now), row["sticky_binding_id"]))
                    self._event(
                        db, outcome="INTERRUPTED", decision_id=decision_id,
                        binding_id=row["sticky_binding_id"], environment_id=environment_id,
                        requested_model=requested_model, selected_channel=row["selected_channel"],
                        reason=failure, gate_trace=gate_trace, evidence=evidence, is_mock=is_mock,
                    )
                    updated = db.execute(
                        "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?",
                        (row["sticky_binding_id"],),
                    ).fetchone()
                    response = {"outcome": "INTERRUPTED", "reason": failure,
                                "binding": self._public(updated), "gate_trace": gate_trace}
                    self._decision_store(
                        db, decision_id, "resolve", resolve_hash, response)
                    db.commit()
                    return response
                self._event(
                    db, outcome="BLOCKED", decision_id=decision_id, binding_id=None,
                    environment_id=environment_id, requested_model=requested_model,
                    selected_channel=None, reason=failure, gate_trace=gate_trace,
                    evidence=evidence, is_mock=is_mock,
                )
                response = {"outcome": "BLOCKED", "reason": failure, "gate_trace": gate_trace}
                self._decision_store(
                    db, decision_id, "resolve", resolve_hash, response)
                db.commit()
                return response
            if row and row["selected_channel"] not in eligible:
                reason = "bound_channel_no_longer_eligible"
                db.execute("""UPDATE sticky_bindings SET state='INTERRUPTED',
                  interruption_reason=?,latest_decision_id=?,audit_timestamp=?,
                  revision=revision+1 WHERE sticky_binding_id=? AND state='ACTIVE'""",
                  (reason, decision_id, _iso(now), row["sticky_binding_id"]))
                self._event(
                    db, outcome="INTERRUPTED", decision_id=decision_id,
                    binding_id=row["sticky_binding_id"], environment_id=environment_id,
                    requested_model=requested_model, selected_channel=row["selected_channel"],
                    reason=reason, gate_trace=gate_trace, evidence=evidence, is_mock=is_mock,
                )
                updated = db.execute(
                    "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?",
                    (row["sticky_binding_id"],),
                ).fetchone()
                response = {"outcome": "INTERRUPTED", "reason": reason,
                            "binding": self._public(updated), "gate_trace": gate_trace}
                self._decision_store(
                    db, decision_id, "resolve", resolve_hash, response)
                db.commit()
                return response
            if row:
                maximum = _parse(row["maximum_expires_at"])
                effective_now = max(now, _parse(row["last_used_at"]))
                expires = max(
                    _parse(row["expires_at"]),
                    min(
                        effective_now + timedelta(
                            seconds=int(self.policy["ttl_seconds"])),
                        maximum,
                    ),
                )
                db.execute("""UPDATE sticky_bindings SET expires_at=?,last_used_at=?,
                  hit_count=hit_count+1,latest_decision_id=?,audit_timestamp=?,
                  revision=revision+1 WHERE sticky_binding_id=? AND state='ACTIVE'""",
                  (_iso(expires), _iso(effective_now), decision_id, _iso(now),
                   row["sticky_binding_id"]))
                self._event(
                    db, outcome="HIT", decision_id=decision_id, binding_id=row["sticky_binding_id"],
                    environment_id=environment_id, requested_model=requested_model,
                    selected_channel=row["selected_channel"], reason=None,
                    gate_trace=gate_trace, evidence=evidence, is_mock=is_mock,
                )
                updated = db.execute(
                    "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?",
                    (row["sticky_binding_id"],),
                ).fetchone()
                response = {"outcome": "HIT", "selected_channel": row["selected_channel"],
                            "binding": self._public(updated), "gate_trace": gate_trace}
                self._decision_store(
                    db, decision_id, "resolve", resolve_hash, response)
                db.commit()
                return response
            self._event(
                db, outcome="MISS", decision_id=decision_id, binding_id=None,
                environment_id=environment_id, requested_model=requested_model,
                selected_channel=None, reason=None, gate_trace=gate_trace,
                evidence=evidence, is_mock=is_mock,
            )
            response = {
            "outcome": "MISS", "safe_route_key_fingerprint": scope.safe_fingerprint,
            "capability_scope_hash": scope.capability_scope_hash,
            "gate_trace": gate_trace,
            }
            db.execute("""INSERT OR REPLACE INTO sticky_miss_authorizations(
              decision_id,route_key_fingerprint,eligible_channel_ids_json,
              created_at,consumed_at) VALUES(?,?,?,?,NULL)""", (
                decision_id, scope.route_key_fingerprint,
                json.dumps(sorted(eligible), separators=(",", ":")),
                _iso(now),
            ))
            self._decision_store(
                db, decision_id, "resolve", resolve_hash, response)
            db.commit()
        return response

    def create_binding(
        self, *, decision_id: str, affinity: str, environment_id: str,
        requested_model: str, capability_scope: Mapping[str, Any] | None,
        stream: bool, selected_channel: str, evidence: Mapping[str, Any] | None = None,
        is_mock: bool = True,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        tenant_scope = self._tenant_scope(
            principal, "scheduler.decide", scheduler_only=True)
        if not self.operational:
            raise StickyRoutingError("sticky_service_not_operational")
        decision_id = _safe_identifier(decision_id, "decision_id")
        environment_id = _safe_identifier(environment_id, "environment_id")
        requested_model = _safe_identifier(requested_model, "requested_model")
        selected_channel = _safe_identifier(selected_channel, "selected_channel")
        if self.authorization_service is not None:
            affinity = f"{self._scope_token(tenant_scope)}:{affinity}"
            decision_id = f"{self._scope_token(tenant_scope)}:{decision_id}"
        scope = self._scope(
            affinity=affinity, environment_id=environment_id,
            requested_model=requested_model, capability_scope=capability_scope, stream=stream,
        )
        now = self.clock()
        expires = now + timedelta(seconds=int(self.policy["ttl_seconds"]))
        maximum = now + timedelta(seconds=int(self.policy["maximum_total_duration_seconds"]))
        metric_id, confidence_id, ids = self._evidence(evidence)
        binding_id = (f"STB-{self._scope_token(tenant_scope)}-{uuid.uuid4().hex}"
                      if self.authorization_service is not None
                      else f"STB-{uuid.uuid4().hex}")
        create_hash = self._decision_hash({
            "route": scope.route_key_fingerprint,
            "selected_channel": selected_channel,
        })
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._decision_replay(
                db, decision_id, "create", create_hash)
            if replay is not None:
                db.commit()
                return replay
            authorization = db.execute("""SELECT * FROM sticky_miss_authorizations
              WHERE decision_id=?""", (decision_id,)).fetchone()
            if (
                not authorization
                or authorization["consumed_at"] is not None
                or authorization["route_key_fingerprint"] != scope.route_key_fingerprint
                or selected_channel not in json.loads(
                    authorization["eligible_channel_ids_json"])
                or (
                    now - _parse(authorization["created_at"])
                ).total_seconds() > int(
                    self.policy.get("miss_authorization_seconds", 60))
            ):
                db.rollback()
                raise StickyRoutingError("sticky_miss_authorization_invalid")
            db.execute("""UPDATE sticky_miss_authorizations SET consumed_at=?
              WHERE decision_id=? AND consumed_at IS NULL""",
              (_iso(now), decision_id))
            existing = db.execute(
                "SELECT * FROM sticky_bindings WHERE route_key_fingerprint=? AND state='ACTIVE'",
                (scope.route_key_fingerprint,),
            ).fetchone()
            if existing:
                response = {
                    "outcome": "HIT", "created": False,
                    "binding": self._public(existing)}
                self._decision_store(
                    db, decision_id, "create", create_hash, response)
                db.commit()
                return response
            db.execute("""INSERT INTO sticky_bindings(
              sticky_binding_id,route_key_fingerprint,affinity_fingerprint,
              safe_route_key_fingerprint,
              route_key_id,route_key_derivation_version,capability_scope_version,
              environment_id,requested_model,
              capability_scope_json,capability_scope_hash,selected_channel,state,
              policy_version,configuration_version,created_at,expires_at,
              maximum_expires_at,last_used_at,hit_count,originating_decision_id,
              latest_decision_id,metric_snapshot_id,confidence_snapshot_id,
              evidence_ids_json,is_mock,audit_timestamp)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,0,?,?,?,?,?,?,?)""", (
                binding_id, scope.route_key_fingerprint, scope.affinity_fingerprint,
                scope.safe_fingerprint,
                self.key_id, self.policy["route_key_derivation_version"],
                self.policy["capability_scope_version"],
                environment_id, requested_model,
                json.dumps(scope.capability_scope, separators=(",", ":")),
                scope.capability_scope_hash, str(selected_channel),
                self.policy["policy_version"], self.policy["configuration_version"],
                _iso(now), _iso(expires), _iso(maximum), _iso(now),
                decision_id, decision_id, metric_id, confidence_id,
                json.dumps(ids, separators=(",", ":")), int(is_mock), _iso(now),
            ))
            self._event(
                db, outcome="CREATED", decision_id=decision_id, binding_id=binding_id,
                environment_id=environment_id, requested_model=requested_model,
                selected_channel=str(selected_channel), reason=None, gate_trace=[],
                evidence=evidence, is_mock=is_mock,
            )
            row = db.execute(
                "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?", (binding_id,)
            ).fetchone()
            response = {
                "outcome": "CREATED", "created": True,
                "binding": self._public(row)}
            self._decision_store(
                db, decision_id, "create", create_hash, response)
            db.commit()
        return response

    def interrupt(self, binding_id: str, *, decision_id: str, reason: str,
                  principal: PrincipalContext | None = None) -> dict[str, Any] | None:
        tenant_scope = self._tenant_scope(
            principal, "scheduler.execute", scheduler_only=True)
        if (self.authorization_service is not None and not str(binding_id).startswith(
                f"STB-{self._scope_token(tenant_scope)}-")):
            raise PermissionError("tenant_scope_mismatch")
        binding_id = _safe_identifier(binding_id, "binding_id")
        decision_id = _safe_identifier(decision_id, "decision_id")
        reason = _safe_identifier(reason, "interruption_reason")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?", (binding_id,)
            ).fetchone()
            if not row:
                db.rollback()
                return None
            if row["state"] == "ACTIVE":
                db.execute("""UPDATE sticky_bindings SET state='INTERRUPTED',
                  interruption_reason=?,latest_decision_id=?,audit_timestamp=?,
                  revision=revision+1 WHERE sticky_binding_id=? AND state='ACTIVE'""",
                  (reason, decision_id, _iso(self.clock()), binding_id))
                self._event(
                    db, outcome="INTERRUPTED", decision_id=decision_id, binding_id=binding_id,
                    environment_id=row["environment_id"], requested_model=row["requested_model"],
                    selected_channel=row["selected_channel"], reason=reason, gate_trace=[],
                    evidence=None, is_mock=bool(row["is_mock"]),
                )
            updated = db.execute(
                "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?", (binding_id,)
            ).fetchone()
            db.commit()
            return self._public(updated)

    def invalidate(
        self, binding_id: str, *, environment_id: str, reason: str,
        idempotency_key: str, request_sha256: str,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        tenant_scope = self._tenant_scope(principal, "exploration.manage")
        if (self.authorization_service is not None and not str(binding_id).startswith(
                f"STB-{self._scope_token(tenant_scope)}-")):
            raise StickyRoutingError("sticky_binding_not_found")
        binding_id = _safe_identifier(binding_id, "binding_id")
        environment_id = _safe_identifier(environment_id, "environment_id")
        reason = _safe_identifier(reason, "invalidation_reason")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise StickyRoutingError("unsafe_idempotency_key")
        if self.authorization_service is None:
            import hashlib
            idempotency_key = hashlib.sha256(
                idempotency_key.encode("utf-8")).hexdigest()
        else:
            idempotency_key = tenant_scope.idempotency_key(
                operation="sticky_invalidate", external_key=idempotency_key,
                version="v1")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = db.execute(
                "SELECT * FROM sticky_mutation_idempotency WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if replay:
                if replay["request_sha256"] != request_sha256:
                    db.rollback()
                    raise StickyRoutingError("idempotency_key_conflict")
                db.commit()
                response = json.loads(replay["response_json"])
                response["idempotent_replay"] = True
                return response
            row = db.execute(
                "SELECT * FROM sticky_bindings WHERE sticky_binding_id=? AND environment_id=?",
                (binding_id, environment_id),
            ).fetchone()
            if not row:
                db.rollback()
                raise StickyRoutingError("sticky_binding_not_found")
            if row["state"] == "ACTIVE":
                now = _iso(self.clock())
                db.execute("""UPDATE sticky_bindings SET state='INVALIDATED',
                  invalidation_reason=?,audit_timestamp=?,revision=revision+1
                  WHERE sticky_binding_id=? AND state='ACTIVE'""",
                  (reason, now, binding_id))
                self._event(
                    db, outcome="INVALIDATED", decision_id=None, binding_id=binding_id,
                    environment_id=row["environment_id"], requested_model=row["requested_model"],
                    selected_channel=row["selected_channel"], reason=reason, gate_trace=[],
                    evidence=None, is_mock=bool(row["is_mock"]),
                )
            updated = db.execute(
                "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?", (binding_id,)
            ).fetchone()
            response = {"status": "ready", "binding": self._public(updated), "idempotent_replay": False}
            db.execute("""INSERT INTO sticky_mutation_idempotency(
              idempotency_key,request_sha256,response_json,created_at) VALUES(?,?,?,?)""",
              (idempotency_key, request_sha256,
               json.dumps(response, ensure_ascii=False, separators=(",", ":")),
               _iso(self.clock())),
            )
            db.commit()
            return response

    def get(self, binding_id: str, *,
            principal: PrincipalContext | None = None) -> dict[str, Any] | None:
        tenant_scope = self._tenant_scope(principal, "decision.read")
        if (self.authorization_service is not None and not str(binding_id).startswith(
                f"STB-{self._scope_token(tenant_scope)}-")):
            return None
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM sticky_bindings WHERE sticky_binding_id=?", (binding_id,)
            ).fetchone()
            return self._public(row) if row else None

    def list(
        self, *, environment_id: str | None = None, state: str | None = None,
        model: str | None = None, channel: str | None = None,
        limit: int = 50, offset: int = 0,
        principal: PrincipalContext | None = None,
    ) -> dict[str, Any]:
        tenant_scope = self._tenant_scope(principal, "decision.read")
        clauses, values = [], []
        if self.authorization_service is not None:
            clauses.append("sticky_binding_id LIKE ?")
            values.append(f"STB-{self._scope_token(tenant_scope)}-%")
        for column, value in (
            ("environment_id", environment_id),
            ("requested_model", model), ("selected_channel", channel),
        ):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        now = _iso(self.clock())
        if state == "ACTIVE":
            clauses.append(
                "state='ACTIVE' AND expires_at>? AND maximum_expires_at>?")
            values.extend([now, now])
        elif state == "EXPIRED":
            clauses.append(
                "(state='EXPIRED' OR (state='ACTIVE' AND "
                "(expires_at<=? OR maximum_expires_at<=?)))")
            values.extend([now, now])
        elif state:
            clauses.append("state=?")
            values.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connection() as db:
            total = db.execute(
                f"SELECT COUNT(*) FROM sticky_bindings{where}", values
            ).fetchone()[0]
            rows = db.execute(
                f"""SELECT * FROM sticky_bindings{where}
                    ORDER BY created_at DESC,sticky_binding_id ASC LIMIT ? OFFSET ?""",
                [*values, limit, offset],
            ).fetchall()
        return {
            "status": "ready", "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total,
            "items": [self._public(row) for row in rows],
        }

    def status(self, *, principal: PrincipalContext | None = None) -> dict[str, Any]:
        tenant_scope = self._tenant_scope(principal, "decision.read")
        with self._connection() as db:
            if self.authorization_service is None:
                rows = db.execute(
                    "SELECT state,expires_at,maximum_expires_at FROM sticky_bindings"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT state,expires_at,maximum_expires_at FROM sticky_bindings "
                    "WHERE sticky_binding_id LIKE ?",
                    (f"STB-{self._scope_token(tenant_scope)}-%",),
                ).fetchall()
            counts: dict[str, int] = {}
            now = self.clock()
            for row in rows:
                effective = row["state"]
                if effective == "ACTIVE" and (
                    _parse(row["expires_at"]) <= now
                    or _parse(row["maximum_expires_at"]) <= now
                ):
                    effective = "EXPIRED"
                counts[effective] = counts.get(effective, 0) + 1
            migration = db.execute(
                "SELECT MAX(version) FROM sticky_schema_migrations"
            ).fetchone()[0]
        blocked_reason = None
        if not self.policy["enabled"]:
            blocked_reason = "sticky_policy_disabled"
        elif not self.secret or not self.key_id:
            blocked_reason = "sticky_key_material_unavailable"
        elif not self._eligibility_provider_configured:
            blocked_reason = "sticky_eligibility_provider_unavailable"
        execution_ready = bool(
            self.operational and self._eligibility_provider_configured)
        return {
            "status": "ready" if execution_ready else "blocked",
            "blocked_reason": blocked_reason,
            "policy_version": self.policy["policy_version"],
            "configuration_version": self.policy["configuration_version"],
            "enabled": bool(self.policy["enabled"]),
            "operational": self.operational,
            "execution_ready": execution_ready,
            "ttl_seconds": int(self.policy["ttl_seconds"]),
            "maximum_total_duration_seconds": int(
                self.policy["maximum_total_duration_seconds"]),
            "route_key_derivation_version": self.policy["route_key_derivation_version"],
            "route_key_id": self.key_id or None,
            "capability_scope_version": self.policy["capability_scope_version"],
            "required_gate_order": list(GATE_ORDER),
            "state_counts": counts,
            "schema_version": migration,
            "network_called": False,
        }

    def metrics(self, *, environment_id: str | None = None,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        tenant_scope = self._tenant_scope(principal, "decision.read")
        clauses: list[str] = []
        values: list[Any] = []
        if self.authorization_service is not None:
            token = self._scope_token(tenant_scope)
            clauses.append("(sticky_binding_id LIKE ? OR decision_id LIKE ?)")
            values.extend([f"STB-{token}-%", f"{token}:%"])
        if environment_id:
            clauses.append("environment_id=?")
            values.append(environment_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connection() as db:
            counts = {
                row["outcome"]: row["count"] for row in db.execute(
                    f"SELECT outcome,COUNT(*) count FROM sticky_routing_events{where} GROUP BY outcome",
                    values,
                )
            }
            reasons = [{
                "reason": row["reason"], "count": row["count"]
            } for row in db.execute(
                f"""SELECT reason,COUNT(*) count FROM sticky_routing_events{where}
                    {'AND' if where else 'WHERE'} outcome='INTERRUPTED'
                    GROUP BY reason ORDER BY count DESC,reason ASC""", values
            )]
        hits, misses = counts.get("HIT", 0), counts.get("MISS", 0)
        denominator = hits + misses
        return {
            "status": "ready", "environment_id": environment_id,
            "outcomes": counts,
            "hit_rate": (hits / denominator) if denominator else None,
            "interruption_reasons": reasons,
            "network_called": False,
        }
