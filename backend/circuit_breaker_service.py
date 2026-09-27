"""Persistent, process-safe circuit breaker backed by SQLite.

The service performs no I/O other than local SQLite access.  Admission and
outcome methods use ``BEGIN IMMEDIATE`` so multiple workers cannot oversubscribe
half-open probes or race state transitions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext, PrincipalType
from backend.tenant_security import TenantScope


STATES = frozenset({"CLOSED", "OPEN", "HALF_OPEN"})


class CircuitBreakerError(ValueError):
    """Base error for invalid circuit-breaker operations."""


class CircuitBreakerPolicyError(CircuitBreakerError):
    pass


class CircuitBreakerEventConflict(CircuitBreakerError):
    pass


class CircuitBreakerLeaseError(CircuitBreakerError):
    pass


class CircuitBreakerCorruptState(CircuitBreakerError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_policy(path_or_policy: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(path_or_policy, Mapping):
        policy = dict(path_or_policy)
    else:
        try:
            policy = json.loads(Path(path_or_policy).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CircuitBreakerPolicyError("circuit_breaker_policy_unreadable") from exc
    integer_fields = (
        "failure_window_seconds", "failure_threshold", "cooldown_seconds",
        "half_open_probe_quota", "half_open_success_threshold", "probe_lease_seconds",
    )
    if not isinstance(policy.get("enabled"), bool):
        raise CircuitBreakerPolicyError("circuit_breaker_policy_enabled_invalid")
    if not isinstance(policy.get("policy_version"), str) or not policy["policy_version"].strip():
        raise CircuitBreakerPolicyError("circuit_breaker_policy_version_invalid")
    for field in integer_fields:
        value = policy.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CircuitBreakerPolicyError(f"circuit_breaker_policy_{field}_invalid")
    return policy


class CircuitBreakerService:
    """Atomic persistent circuit breaker.

    ``circuit_id`` is an opaque caller-defined scope (typically an environment
    and channel tuple). ``request_id`` and ``event_id`` must be stable IDs so
    retries can return the original result without applying an event twice.
    """

    def __init__(
        self,
        database_path: str | Path,
        policy: str | Path | Mapping[str, Any],
        *,
        clock: Callable[[], datetime] = _utc_now,
        authorization_service: AuthorizationService | None = None,
    ) -> None:
        self.path = Path(database_path)
        self.policy = load_policy(policy)
        self.clock = clock
        self.authorization_service = authorization_service
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self.recover()

    def _scope(self, principal: PrincipalContext | None, permission: str) -> TenantScope:
        if self.authorization_service is None:
            return TenantScope.local_development()
        verified = self.authorization_service.authorize(principal, permission)
        if permission == "scheduler.execute":
            if (verified.principal_type is not PrincipalType.SERVICE
                    or "scheduler_service" not in verified.roles):
                raise PermissionError("scheduler_service_identity_required")
        return TenantScope(verified.tenant_id, verified.workspace_id)

    def _scoped_id(self, value: Any, name: str, scope: TenantScope) -> str:
        logical = self._id(value, name)
        if self.authorization_service is None:
            return logical
        digest = hashlib.sha256(
            f"{scope.tenant_id}\x00{scope.workspace_id}".encode()).hexdigest()[:24]
        value_digest = hashlib.sha256(logical.encode()).hexdigest()
        return f"ent:{digest}:{value_digest}"

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

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise CircuitBreakerError("circuit_breaker_clock_must_be_timezone_aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _id(value: Any, name: str) -> str:
        text = str(value or "").strip()
        if not text or len(text) > 240 or any(ord(char) < 32 for char in text):
            raise CircuitBreakerError(f"invalid_{name}")
        return text

    def _migrate(self) -> None:
        now = _iso(self._now())
        with self._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS circuit_breakers(
              circuit_id TEXT PRIMARY KEY,
              state TEXT NOT NULL CHECK(state IN ('CLOSED','OPEN','HALF_OPEN')),
              opened_at TEXT,
              cooldown_until TEXT,
              half_open_successes INTEGER NOT NULL DEFAULT 0 CHECK(half_open_successes >= 0),
              revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
              policy_version TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS circuit_failure_events(
              event_id TEXT PRIMARY KEY,
              circuit_id TEXT NOT NULL REFERENCES circuit_breakers(circuit_id),
              occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_circuit_failures_scope_time
              ON circuit_failure_events(circuit_id,occurred_at);
            CREATE TABLE IF NOT EXISTS circuit_probe_leases(
              lease_id TEXT PRIMARY KEY,
              circuit_id TEXT NOT NULL REFERENCES circuit_breakers(circuit_id),
              request_id TEXT NOT NULL UNIQUE,
              state TEXT NOT NULL CHECK(state IN ('ACTIVE','CONSUMED','EXPIRED','CANCELLED')),
              acquired_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              consumed_at TEXT,
              outcome_event_id TEXT UNIQUE
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_active_probe_request
              ON circuit_probe_leases(circuit_id,request_id) WHERE state='ACTIVE';
            CREATE INDEX IF NOT EXISTS ix_probe_scope_state_expiry
              ON circuit_probe_leases(circuit_id,state,expires_at);
            CREATE TABLE IF NOT EXISTS circuit_idempotency(
              event_id TEXT PRIMARY KEY,
              operation TEXT NOT NULL,
              request_hash TEXT NOT NULL,
              response_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS circuit_transition_audits(
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
              circuit_id TEXT NOT NULL,
              from_state TEXT,
              to_state TEXT NOT NULL,
              reason TEXT NOT NULL,
              event_id TEXT,
              revision INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_circuit_audit_scope_time
              ON circuit_transition_audits(circuit_id,audit_id);
            """)
            result = db.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise CircuitBreakerCorruptState("circuit_breaker_database_corrupt")
            db.execute("""CREATE TABLE IF NOT EXISTS circuit_schema_migrations(
              version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO circuit_schema_migrations VALUES(1,?)", (now,))

    def _create(self, db: sqlite3.Connection, circuit_id: str, now: datetime) -> sqlite3.Row:
        stamp = _iso(now)
        db.execute("""INSERT OR IGNORE INTO circuit_breakers(
          circuit_id,state,policy_version,updated_at) VALUES(?,'CLOSED',?,?)""",
                   (circuit_id, self.policy["policy_version"], stamp))
        row = db.execute("SELECT * FROM circuit_breakers WHERE circuit_id=?", (circuit_id,)).fetchone()
        assert row is not None
        return row

    @staticmethod
    def _audit(db: sqlite3.Connection, circuit_id: str, before: str | None,
               after: str, reason: str, event_id: str | None,
               revision: int, now: datetime) -> None:
        db.execute("""INSERT INTO circuit_transition_audits(
          circuit_id,from_state,to_state,reason,event_id,revision,created_at)
          VALUES(?,?,?,?,?,?,?)""",
                   (circuit_id, before, after, reason, event_id, revision, _iso(now)))

    def _force_safe_open(self, db: sqlite3.Connection, row: sqlite3.Row,
                         reason: str, now: datetime) -> sqlite3.Row:
        revision = int(row["revision"]) + 1
        until = now + timedelta(seconds=self.policy["cooldown_seconds"])
        db.execute("""UPDATE circuit_breakers SET state='OPEN',opened_at=?,cooldown_until=?,
          half_open_successes=0,revision=?,policy_version=?,updated_at=? WHERE circuit_id=?""",
                   (_iso(now), _iso(until), revision, self.policy["policy_version"],
                    _iso(now), row["circuit_id"]))
        db.execute("""UPDATE circuit_probe_leases SET state='CANCELLED'
          WHERE circuit_id=? AND state='ACTIVE'""", (row["circuit_id"],))
        self._audit(db, row["circuit_id"], row["state"], "OPEN", reason, None, revision, now)
        return db.execute("SELECT * FROM circuit_breakers WHERE circuit_id=?",
                          (row["circuit_id"],)).fetchone()

    def _validated(self, db: sqlite3.Connection, circuit_id: str, now: datetime) -> sqlite3.Row:
        row = self._create(db, circuit_id, now)
        state = row["state"]
        active_leases = db.execute("""SELECT COUNT(*) FROM circuit_probe_leases
          WHERE circuit_id=? AND state='ACTIVE'""", (circuit_id,)).fetchone()[0]
        inconsistent = (
            state not in STATES
            or (state == "CLOSED" and (row["opened_at"] is not None or row["cooldown_until"] is not None
                                       or row["half_open_successes"] != 0))
            or (state == "OPEN" and (row["opened_at"] is None or row["cooldown_until"] is None
                                     or row["half_open_successes"] != 0))
            or (state == "HALF_OPEN" and (row["opened_at"] is None or row["cooldown_until"] is not None
                                          or row["half_open_successes"] < 0
                                          or row["half_open_successes"] >= self.policy["half_open_success_threshold"]
                                          or active_leases > self.policy["half_open_probe_quota"]))
            or (state != "HALF_OPEN" and active_leases != 0)
        )
        if inconsistent:
            row = self._force_safe_open(db, row, "corrupt_state_fail_closed", now)
        return row

    def _transition(self, db: sqlite3.Connection, row: sqlite3.Row, state: str,
                    reason: str, now: datetime, event_id: str | None = None) -> sqlite3.Row:
        before = row["state"]
        revision = int(row["revision"]) + 1
        opened_at = _iso(now) if state == "OPEN" else (row["opened_at"] if state == "HALF_OPEN" else None)
        cooldown = _iso(now + timedelta(seconds=self.policy["cooldown_seconds"])) if state == "OPEN" else None
        db.execute("""UPDATE circuit_breakers SET state=?,opened_at=?,cooldown_until=?,
          half_open_successes=0,revision=?,policy_version=?,updated_at=? WHERE circuit_id=?""",
                   (state, opened_at, cooldown, revision, self.policy["policy_version"],
                    _iso(now), row["circuit_id"]))
        if state != "HALF_OPEN":
            db.execute("""UPDATE circuit_probe_leases SET state='CANCELLED'
              WHERE circuit_id=? AND state='ACTIVE'""", (row["circuit_id"],))
        if state == "CLOSED":
            db.execute("DELETE FROM circuit_failure_events WHERE circuit_id=?", (row["circuit_id"],))
        self._audit(db, row["circuit_id"], before, state, reason, event_id, revision, now)
        return db.execute("SELECT * FROM circuit_breakers WHERE circuit_id=?",
                          (row["circuit_id"],)).fetchone()

    def _expire_leases(self, db: sqlite3.Connection, circuit_id: str, now: datetime) -> None:
        db.execute("""UPDATE circuit_probe_leases SET state='EXPIRED'
          WHERE circuit_id=? AND state='ACTIVE' AND expires_at<=?""", (circuit_id, _iso(now)))

    @staticmethod
    def _hash(payload: Mapping[str, Any]) -> str:
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode("ascii")).hexdigest()

    def _replay(self, db: sqlite3.Connection, event_id: str, operation: str,
                payload: Mapping[str, Any]) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM circuit_idempotency WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != self._hash(payload):
            raise CircuitBreakerEventConflict("circuit_breaker_event_id_conflict")
        return json.loads(row["response_json"])

    def _remember(self, db: sqlite3.Connection, event_id: str, operation: str,
                  payload: Mapping[str, Any], response: Mapping[str, Any], now: datetime) -> None:
        # Enterprise tenant migrations append scope columns with safe defaults.
        # Always name legacy columns so the service remains compatible with both
        # the original five-column table and the scoped seven-column table.
        db.execute("""INSERT INTO circuit_idempotency(
          event_id,operation,request_hash,response_json,created_at) VALUES(?,?,?,?,?)""", (
            event_id, operation, self._hash(payload),
            json.dumps(dict(response), sort_keys=True, separators=(",", ":")), _iso(now)))

    @staticmethod
    def _view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "circuit_id": row["circuit_id"], "state": row["state"],
            "opened_at": row["opened_at"], "cooldown_until": row["cooldown_until"],
            "half_open_successes": row["half_open_successes"], "revision": row["revision"],
            "policy_version": row["policy_version"], "updated_at": row["updated_at"],
        }

    def get_state(self, circuit_id: str, *,
                  principal: PrincipalContext | None = None) -> dict[str, Any]:
        logical_id = self._id(circuit_id, "circuit_id")
        scope = self._scope(principal, "circuit.read")
        circuit_id = self._scoped_id(logical_id, "circuit_id", scope)
        now = self._now()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._validated(db, circuit_id, now)
            if row["state"] == "OPEN" and _parse(row["cooldown_until"]) <= now:
                row = self._transition(db, row, "HALF_OPEN", "cooldown_elapsed", now)
            self._expire_leases(db, circuit_id, now)
            db.commit()
            return {**self._view(row), "circuit_id": logical_id,
                    "tenant_id": scope.tenant_id,
                    "workspace_id": scope.workspace_id}

    def before_request(self, circuit_id: str, request_id: str, *,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._scope(principal, "scheduler.execute")
        circuit_id = self._scoped_id(circuit_id, "circuit_id", scope)
        request_id = self._scoped_id(request_id, "request_id", scope)
        now = self._now()
        payload = {"circuit_id": circuit_id, "request_id": request_id}
        idem_id = f"admit:{request_id}"
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, idem_id, "before_request", payload)
            if replay is not None:
                db.commit()
                return replay
            row = self._validated(db, circuit_id, now)
            if row["state"] == "OPEN" and _parse(row["cooldown_until"]) <= now:
                row = self._transition(db, row, "HALF_OPEN", "cooldown_elapsed", now)
            self._expire_leases(db, circuit_id, now)
            if not self.policy["enabled"]:
                response = {"allowed": True, "state": row["state"], "reason": "disabled", "probe_lease_id": None}
            elif row["state"] == "CLOSED":
                response = {"allowed": True, "state": "CLOSED", "reason": "circuit_closed", "probe_lease_id": None}
            elif row["state"] == "OPEN":
                response = {"allowed": False, "state": "OPEN", "reason": "cooldown_active", "probe_lease_id": None}
            else:
                active = db.execute("""SELECT COUNT(*) FROM circuit_probe_leases
                  WHERE circuit_id=? AND state='ACTIVE'""", (circuit_id,)).fetchone()[0]
                if active >= self.policy["half_open_probe_quota"]:
                    response = {"allowed": False, "state": "HALF_OPEN", "reason": "probe_quota_exhausted", "probe_lease_id": None}
                else:
                    lease_id = str(uuid.uuid4())
                    expires = now + timedelta(seconds=self.policy["probe_lease_seconds"])
                    db.execute("""INSERT INTO circuit_probe_leases(
                      lease_id,circuit_id,request_id,state,acquired_at,expires_at)
                      VALUES(?,?,?,'ACTIVE',?,?)""",
                               (lease_id, circuit_id, request_id, _iso(now), _iso(expires)))
                    response = {"allowed": True, "state": "HALF_OPEN", "reason": "probe_granted",
                                "probe_lease_id": lease_id, "probe_lease_expires_at": _iso(expires)}
            self._remember(db, idem_id, "before_request", payload, response, now)
            db.commit()
            return response

    allow_request = before_request

    def _record(self, operation: str, circuit_id: str, event_id: str,
                probe_lease_id: str | None,
                principal: PrincipalContext | None) -> dict[str, Any]:
        scope = self._scope(principal, "scheduler.execute")
        logical_id = self._id(circuit_id, "circuit_id")
        circuit_id = self._scoped_id(logical_id, "circuit_id", scope)
        event_id = self._scoped_id(event_id, "event_id", scope)
        if probe_lease_id is not None:
            probe_lease_id = self._id(probe_lease_id, "probe_lease_id")
        now = self._now()
        payload = {"circuit_id": circuit_id, "probe_lease_id": probe_lease_id}
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            replay = self._replay(db, event_id, operation, payload)
            if replay is not None:
                db.commit()
                return replay
            row = self._validated(db, circuit_id, now)
            self._expire_leases(db, circuit_id, now)
            if row["state"] == "HALF_OPEN":
                if not probe_lease_id:
                    raise CircuitBreakerLeaseError("half_open_outcome_requires_probe_lease")
                lease = db.execute("SELECT * FROM circuit_probe_leases WHERE lease_id=?",
                                   (probe_lease_id,)).fetchone()
                if lease is None or lease["circuit_id"] != circuit_id:
                    raise CircuitBreakerLeaseError("probe_lease_not_found")
                if lease["state"] != "ACTIVE" or _parse(lease["expires_at"]) <= now:
                    raise CircuitBreakerLeaseError("probe_lease_not_active")
                db.execute("""UPDATE circuit_probe_leases SET state='CONSUMED',consumed_at=?,
                  outcome_event_id=? WHERE lease_id=? AND state='ACTIVE'""",
                           (_iso(now), event_id, probe_lease_id))
                if operation == "failure":
                    row = self._transition(db, row, "OPEN", "half_open_probe_failed", now, event_id)
                else:
                    successes = int(row["half_open_successes"]) + 1
                    if successes >= self.policy["half_open_success_threshold"]:
                        row = self._transition(db, row, "CLOSED", "half_open_success_threshold", now, event_id)
                    else:
                        db.execute("""UPDATE circuit_breakers SET half_open_successes=?,revision=revision+1,
                          updated_at=? WHERE circuit_id=?""", (successes, _iso(now), circuit_id))
                        row = db.execute("SELECT * FROM circuit_breakers WHERE circuit_id=?", (circuit_id,)).fetchone()
            elif probe_lease_id is not None:
                raise CircuitBreakerLeaseError("probe_lease_not_permitted_in_current_state")
            elif row["state"] == "CLOSED" and operation == "failure":
                cutoff = _iso(now - timedelta(seconds=self.policy["failure_window_seconds"]))
                db.execute("DELETE FROM circuit_failure_events WHERE circuit_id=? AND occurred_at<?",
                           (circuit_id, cutoff))
                db.execute("""INSERT INTO circuit_failure_events(
                  event_id,circuit_id,occurred_at) VALUES(?,?,?)""",
                           (event_id, circuit_id, _iso(now)))
                count = db.execute("SELECT COUNT(*) FROM circuit_failure_events WHERE circuit_id=?",
                                   (circuit_id,)).fetchone()[0]
                if count >= self.policy["failure_threshold"]:
                    row = self._transition(db, row, "OPEN", "failure_threshold_reached", now, event_id)
            elif row["state"] == "OPEN":
                raise CircuitBreakerError("outcome_not_accepted_while_open")
            response = self._view(row)
            response["circuit_id"] = logical_id
            response["tenant_id"] = scope.tenant_id
            response["workspace_id"] = scope.workspace_id
            response["accepted"] = True
            self._remember(db, event_id, operation, payload, response, now)
            db.commit()
            return response

    def record_success(self, circuit_id: str, event_id: str,
                       probe_lease_id: str | None = None, *,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        return self._record("success", circuit_id, event_id, probe_lease_id, principal)

    def record_failure(self, circuit_id: str, event_id: str,
                       probe_lease_id: str | None = None, *,
                       principal: PrincipalContext | None = None) -> dict[str, Any]:
        return self._record("failure", circuit_id, event_id, probe_lease_id, principal)

    def recover(self) -> dict[str, int]:
        """Expire stale leases and quarantine inconsistent rows as OPEN."""
        now, expired, quarantined = self._now(), 0, 0
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                db.rollback()
                raise CircuitBreakerCorruptState("circuit_breaker_database_corrupt")
            for row in db.execute("SELECT * FROM circuit_breakers").fetchall():
                validated = self._validated(db, row["circuit_id"], now)
                quarantined += int(validated["revision"] != row["revision"])
            cursor = db.execute("""UPDATE circuit_probe_leases SET state='EXPIRED'
              WHERE state='ACTIVE' AND expires_at<=?""", (_iso(now),))
            expired = cursor.rowcount
            db.commit()
        return {"expired_probe_leases": expired, "quarantined_circuits": quarantined}

    def list_transition_audits(self, circuit_id: str, *,
                               principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._scope(principal, "circuit.read")
        circuit_id = self._scoped_id(circuit_id, "circuit_id", scope)
        with self._connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM circuit_transition_audits WHERE circuit_id=? ORDER BY audit_id",
                (circuit_id,)).fetchall()]

    def list_states(self, *, limit: int = 100,
                    principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Return a bounded read-only operational view for monitoring."""
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise CircuitBreakerError("invalid_circuit_state_limit")
        scope = self._scope(principal, "circuit.read")
        self.recover()
        with self._connection() as db:
            if self.authorization_service is None:
                prefix, params = "%", ()
            else:
                digest = hashlib.sha256(
                    f"{scope.tenant_id}\x00{scope.workspace_id}".encode()).hexdigest()[:24]
                prefix, params = f"ent:{digest}:%", ()
            rows = db.execute(
                "SELECT * FROM circuit_breakers WHERE circuit_id LIKE ? "
                "ORDER BY updated_at DESC,circuit_id LIMIT ?",
                (prefix, limit)).fetchall()
            counts = {row["state"]: row["count"] for row in db.execute(
                "SELECT state,COUNT(*) AS count FROM circuit_breakers "
                "WHERE circuit_id LIKE ? GROUP BY state", (prefix,)).fetchall()}
            active_probes = db.execute(
                "SELECT COUNT(*) FROM circuit_probe_leases WHERE state='ACTIVE' "
                "AND circuit_id LIKE ?", (prefix,)).fetchone()[0]
        return {
            "status": "ready", "policy_version": self.policy["policy_version"],
            "enabled": self.policy["enabled"], "state_counts": counts,
            "active_probe_leases": active_probes,
            "items": [self._view(row) for row in rows],
            "tenant_id": scope.tenant_id,
            "workspace_id": scope.workspace_id,
            "network_called": False,
        }
