"""Versioned, evidence-only capability registry with fail-closed resolution."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from backend.security.authorization import AuthorizationService
from backend.security.principal import PrincipalContext
from backend.tenant_security import TenantScope


class CapabilityEvidenceError(ValueError):
    pass


_PROTECTED_DETAIL_TERMS = ("authorization", "api_key", "password", "secret", "token",
                           "cookie", "prompt", "response_body", "dpapi")


def _has_protected_detail(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(any(term in str(key).casefold() for term in _PROTECTED_DETAIL_TERMS)
                   or _has_protected_detail(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_protected_detail(item) for item in value)
    return False


def _redact_details(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): ("<redacted>" if any(term in str(key).casefold()
                for term in _PROTECTED_DETAIL_TERMS) else _redact_details(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_details(item) for item in value]
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_capability_evidence_policy(path: str | Path) -> dict[str, Any]:
    try:
        policy = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityEvidenceError("invalid_capability_evidence_policy") from exc
    required = {"policy_version", "registry_version", "maximum_evidence_age_seconds",
                "allowed_evidence_types", "allowed_sources", "requirements", "states",
                "fixed_scenarios", "media_bounds"}
    expected_requirements = ["text", "stream", "image_in", "image_out", "audio", "video_in", "tool", "context"]
    expected_states = {"supported", "unsupported", "unknown", "conflicting"}
    if (not isinstance(policy, dict) or not required.issubset(policy)
            or policy["requirements"] != expected_requirements
            or set(policy["states"]) != expected_states
            or not isinstance(policy["maximum_evidence_age_seconds"], int)
            or policy["maximum_evidence_age_seconds"] <= 0
            or policy.get("legacy_assumptions_unverified") is not True):
        raise CapabilityEvidenceError("invalid_capability_evidence_policy")
    if (not policy["fixed_scenarios"] or any(
            not values or not set(values) <= set(expected_requirements)
            for values in policy["fixed_scenarios"].values())):
        raise CapabilityEvidenceError("invalid_capability_scenarios")
    bounds = policy["media_bounds"]
    if any(int(bounds.get(key, 0)) <= 0 for key in (
            "maximum_input_bytes", "maximum_output_bytes", "maximum_context_tokens")):
        raise CapabilityEvidenceError("invalid_capability_bounds")
    policy["_policy_sha256"] = hashlib.sha256(json.dumps(policy, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return policy


class CapabilityEvidenceService:
    """Stores bounded capability claims; identifiers never imply capabilities."""

    def __init__(self, database_path: str | Path, policy_path: str | Path, *,
                 clock: Callable[[], datetime] = _now,
                 authorization_service: AuthorizationService | None = None):
        self.path = Path(database_path)
        self.policy = load_capability_evidence_policy(policy_path)
        self.clock = clock
        self.authorization_service = authorization_service
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _authorize(self, principal: PrincipalContext | None, permission: str) -> TenantScope:
        if self.authorization_service is not None:
            verified = self.authorization_service.authorize(principal, permission)
            return TenantScope(verified.tenant_id, verified.workspace_id)
        return TenantScope.local_development()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def _connection(self):
        db = self.connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init(self) -> None:
        with self._connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS capability_evidence(
              evidence_id TEXT NOT NULL, evidence_type TEXT NOT NULL,
              observed_at TEXT NOT NULL, environment_id TEXT NOT NULL,
              subject_id TEXT NOT NULL, subject_version TEXT NOT NULL,
              source TEXT NOT NULL, requirement TEXT NOT NULL, state TEXT NOT NULL,
              registry_version TEXT NOT NULL, details_json TEXT NOT NULL,
              recorded_at TEXT NOT NULL,tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,evidence_id));
            CREATE INDEX IF NOT EXISTS ix_capability_resolution ON capability_evidence(
              environment_id,subject_id,subject_version,requirement,observed_at DESC);
            CREATE TABLE IF NOT EXISTS capability_evidence_audit(
              audit_id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
              created_at TEXT NOT NULL, details_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1');
            CREATE TABLE IF NOT EXISTS multimodal_validation_runs(
              validation_id TEXT NOT NULL, environment_id TEXT NOT NULL,
              model_id TEXT NOT NULL, channel_id TEXT NOT NULL,
              request_type TEXT NOT NULL, mime_type TEXT NOT NULL,
              input_bytes INTEGER NOT NULL, state TEXT NOT NULL,
              reason TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'tenant_local_dev_v1',
              workspace_id TEXT NOT NULL DEFAULT 'workspace_local_dev_v1',
              PRIMARY KEY(tenant_id,workspace_id,validation_id));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(capability_evidence)")}
            for name, declaration in (
                ("tenant_id", "TEXT NOT NULL DEFAULT 'tenant_local_dev_v1'"),
                ("workspace_id", "TEXT NOT NULL DEFAULT 'workspace_local_dev_v1'"),
                ("scenario_id", "TEXT"), ("mime_types_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("maximum_input_bytes", "INTEGER"), ("maximum_output_bytes", "INTEGER"),
                ("maximum_context_tokens", "INTEGER"), ("valid_until", "TEXT"),
                ("revoked_at", "TEXT"), ("revocation_reason", "TEXT"),
                ("supersedes_evidence_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE capability_evidence ADD COLUMN {name} {declaration}")
            audit_columns = {row[1] for row in db.execute(
                "PRAGMA table_info(capability_evidence_audit)")}
            for name, default in (("tenant_id", "tenant_local_dev_v1"),
                                  ("workspace_id", "workspace_local_dev_v1")):
                if name not in audit_columns:
                    db.execute(f"ALTER TABLE capability_evidence_audit ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_scope_capability_evidence
                        ON capability_evidence(tenant_id,workspace_id)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_scope_capability_evidence_audit
                        ON capability_evidence_audit(tenant_id,workspace_id)""")
            db.execute("""CREATE INDEX IF NOT EXISTS ix_scope_multimodal_validation
                        ON multimodal_validation_runs(tenant_id,workspace_id,created_at DESC)""")

    def record_evidence(self, evidence: Mapping[str, Any], *,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "evidence.import")
        required = {"evidence_id", "evidence_type", "observed_at", "environment_id",
                    "subject_id", "subject_version", "source", "requirement", "state"}
        if not required.issubset(evidence) or any(not str(evidence[k]).strip() for k in required):
            raise CapabilityEvidenceError("incomplete_capability_evidence")
        item = {k: str(evidence[k]).strip() for k in required}
        if item["evidence_type"] not in self.policy["allowed_evidence_types"]:
            raise CapabilityEvidenceError("unknown_evidence_type")
        if item["source"] not in self.policy["allowed_sources"]:
            raise CapabilityEvidenceError("unknown_evidence_source")
        if item["requirement"] not in self.policy["requirements"]:
            raise CapabilityEvidenceError("unknown_capability_requirement")
        if item["state"] not in {"supported", "unsupported"}:
            raise CapabilityEvidenceError("evidence_state_must_be_definite")
        try:
            observed = _parse(item["observed_at"])
        except ValueError as exc:
            raise CapabilityEvidenceError("invalid_evidence_time") from exc
        if observed > self.clock():
            raise CapabilityEvidenceError("future_evidence_time")
        if item["evidence_type"] == "legacy_assumption":
            # Preserve the historical claim but never let it prove a capability.
            item["state"] = "unknown"
        scenario_id = evidence.get("scenario_id")
        if scenario_id is not None and scenario_id not in self.policy["fixed_scenarios"]:
            raise CapabilityEvidenceError("unknown_capability_scenario")
        if scenario_id is not None and item["requirement"] not in self.policy["fixed_scenarios"][scenario_id]:
            raise CapabilityEvidenceError("requirement_not_in_scenario")
        mime_types = sorted(set(str(x).casefold() for x in (evidence.get("mime_types") or [])))
        allowed_mimes = set(self.policy["media_bounds"].get({
            "image_in": "allowed_image_input_mime_types", "image_out": "allowed_image_output_mime_types",
            "audio": "allowed_audio_mime_types",
            "video_in": "allowed_video_input_mime_types"}.get(item["requirement"], ""), []))
        if mime_types and (not allowed_mimes or not set(mime_types) <= allowed_mimes):
            raise CapabilityEvidenceError("unsupported_evidence_mime_type")
        numeric_bounds = {}
        for name in ("maximum_input_bytes", "maximum_output_bytes", "maximum_context_tokens"):
            value = evidence.get(name)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value <= 0
                    or value > self.policy["media_bounds"][name]):
                raise CapabilityEvidenceError(f"invalid_{name}")
            numeric_bounds[name] = value
        valid_until = evidence.get("valid_until")
        if valid_until is not None:
            try: valid_until = _iso(_parse(str(valid_until)))
            except ValueError as exc: raise CapabilityEvidenceError("invalid_evidence_expiry") from exc
            if _parse(valid_until) <= observed: raise CapabilityEvidenceError("invalid_evidence_expiry")
        supersedes = evidence.get("supersedes_evidence_id")
        details = dict(evidence.get("details") or {})
        if _has_protected_detail(details):
            raise CapabilityEvidenceError("protected_evidence_detail_forbidden")
        result = {**item, "observed_at": _iso(observed),
                  "registry_version": self.policy["registry_version"],
                  "recorded_at": _iso(self.clock()),
                  "verified": item["evidence_type"] != "legacy_assumption",
                  "scenario_id": scenario_id, "mime_types": mime_types, **numeric_bounds,
                  "valid_until": valid_until, "supersedes_evidence_id": supersedes}
        with self._connection() as db:
            if supersedes:
                prior = db.execute("SELECT * FROM capability_evidence WHERE evidence_id=? AND tenant_id=? AND workspace_id=?",
                                   (supersedes, *scope.sql_parameters())).fetchone()
                if (not prior or prior["environment_id"] != item["environment_id"]
                        or prior["subject_id"] != item["subject_id"]
                        or prior["subject_version"] != item["subject_version"]
                        or prior["requirement"] != item["requirement"]):
                    raise CapabilityEvidenceError("invalid_superseded_evidence")
            try:
                db.execute("""INSERT INTO capability_evidence(
                  evidence_id,evidence_type,observed_at,environment_id,subject_id,subject_version,
                  source,requirement,state,registry_version,details_json,recorded_at,scenario_id,
                  mime_types_json,maximum_input_bytes,maximum_output_bytes,maximum_context_tokens,
                  valid_until,revoked_at,revocation_reason,supersedes_evidence_id,
                  tenant_id,workspace_id)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?)""",
                           (result["evidence_id"], result["evidence_type"], result["observed_at"],
                            result["environment_id"], result["subject_id"], result["subject_version"],
                            result["source"], result["requirement"], result["state"],
                            result["registry_version"], json.dumps(details, sort_keys=True), result["recorded_at"],
                            scenario_id, json.dumps(mime_types), numeric_bounds["maximum_input_bytes"],
                            numeric_bounds["maximum_output_bytes"], numeric_bounds["maximum_context_tokens"],
                            valid_until, supersedes, *scope.sql_parameters()))
            except sqlite3.IntegrityError as exc:
                raise CapabilityEvidenceError("duplicate_evidence_id") from exc
            db.execute("INSERT INTO capability_evidence_audit(event_type,created_at,details_json,tenant_id,workspace_id) VALUES(?,?,?,?,?)",
                       ("evidence_recorded", result["recorded_at"], json.dumps({"evidence_id": result["evidence_id"]}), *scope.sql_parameters()))
        return result

    def revoke_evidence(self, evidence_id: str, *, reason: str,
                        principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "evidence.import")
        if not reason: raise CapabilityEvidenceError("revocation_reason_required")
        with self._connection() as db:
            cursor = db.execute("UPDATE capability_evidence SET revoked_at=?,revocation_reason=? WHERE evidence_id=? AND revoked_at IS NULL AND tenant_id=? AND workspace_id=?",
                                (_iso(self.clock()), reason, evidence_id, *scope.sql_parameters()))
            if cursor.rowcount != 1: raise CapabilityEvidenceError("evidence_not_active")
            db.execute("INSERT INTO capability_evidence_audit(event_type,created_at,details_json,tenant_id,workspace_id) VALUES(?,?,?,?,?)",
                       ("evidence_revoked", _iso(self.clock()), json.dumps({"evidence_id": evidence_id, "reason": reason}), *scope.sql_parameters()))
        return {"evidence_id": evidence_id, "state": "revoked", "fail_closed": True}

    def resolve(self, *, environment_id: str, subject_id: str, subject_version: str,
                requirement: str, decision_source: str = "live",
                scenario_id: str | None = None,
                request_constraints: Mapping[str, Any] | None = None,
                principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "evidence.read")
        if requirement not in self.policy["requirements"] or decision_source not in self.policy["allowed_sources"]:
            raise CapabilityEvidenceError("invalid_capability_query")
        if scenario_id is not None and (scenario_id not in self.policy["fixed_scenarios"]
                or requirement not in self.policy["fixed_scenarios"][scenario_id]):
            raise CapabilityEvidenceError("invalid_capability_scenario")
        with self._connection() as db:
            rows = db.execute("""SELECT * FROM capability_evidence WHERE environment_id=?
              AND subject_id=? AND subject_version=? AND requirement=?
              AND registry_version=? AND tenant_id=? AND workspace_id=?
              ORDER BY observed_at DESC,evidence_id""",
              (environment_id, subject_id, subject_version, requirement,
               self.policy["registry_version"], *scope.sql_parameters())).fetchall()
        fresh = []; rejected: list[dict[str, str]] = []
        superseded = {row["supersedes_evidence_id"] for row in rows if row["supersedes_evidence_id"]}
        constraints = dict(request_constraints or {})
        max_age = self.policy["maximum_evidence_age_seconds"]
        for row in rows:
            reason = None
            if row["evidence_id"] in superseded: reason = "superseded_evidence"
            elif row["revoked_at"]: reason = "revoked_evidence"
            elif scenario_id is not None and row["scenario_id"] != scenario_id: reason = "scenario_mismatch"
            elif row["evidence_type"] == "legacy_assumption" or row["state"] == "unknown": reason = "unverified_legacy_assumption"
            elif (self.clock() - _parse(row["observed_at"])).total_seconds() > max_age: reason = "stale_evidence"
            elif row["valid_until"] and _parse(row["valid_until"]) <= self.clock(): reason = "expired_evidence"
            elif decision_source == "live" and self.policy.get("require_live_evidence_for_live_decisions", True) and row["source"] != "live": reason = "non_live_evidence"
            elif constraints.get("mime_type") and constraints["mime_type"].casefold() not in json.loads(row["mime_types_json"]): reason = "mime_type_not_evidenced"
            elif constraints.get("input_bytes") and (row["maximum_input_bytes"] is None or constraints["input_bytes"] > row["maximum_input_bytes"]): reason = "input_size_exceeds_evidence"
            elif constraints.get("output_bytes") and (row["maximum_output_bytes"] is None or constraints["output_bytes"] > row["maximum_output_bytes"]): reason = "output_size_exceeds_evidence"
            elif constraints.get("context_tokens") and (row["maximum_context_tokens"] is None or constraints["context_tokens"] > row["maximum_context_tokens"]): reason = "context_exceeds_evidence"
            if reason: rejected.append({"evidence_id": row["evidence_id"], "reason": reason})
            else: fresh.append(row)
        states = {row["state"] for row in fresh}
        state = "conflicting" if len(states) > 1 else (next(iter(states)) if states else "unknown")
        evidence_ids = [row["evidence_id"] for row in fresh]
        result = {"environment_id": environment_id, "subject_id": subject_id,
                  "subject_version": subject_version, "requirement": requirement, "state": state,
                  "supported": state == "supported", "evidence_ids": evidence_ids,
                  "rejected_evidence": rejected, "policy_version": self.policy["policy_version"],
                  "registry_version": self.policy["registry_version"], "fail_closed": state != "supported",
                  "inference_used": False, "network_called": False}
        return result

    def assess_requirements(self, *, environment_id: str, subject_id: str,
                            subject_version: str, requirements: Mapping[str, Any],
                            decision_source: str = "live", scenario_id: str | None = None,
                            request_constraints: Mapping[str, Mapping[str, Any]] | None = None,
                            principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "evidence.read")
        unknown = set(requirements) - set(self.policy["requirements"])
        if unknown: raise CapabilityEvidenceError("unknown_capability_requirement")
        results = {name: self.resolve(environment_id=environment_id, subject_id=subject_id,
                   subject_version=subject_version, requirement=name, decision_source=decision_source,
                   principal=principal)
                   if not scenario_id and not request_constraints else self.resolve(
                   environment_id=environment_id, subject_id=subject_id,
                   subject_version=subject_version, requirement=name, decision_source=decision_source,
                   scenario_id=scenario_id,
                   request_constraints=(request_constraints or {}).get(name), principal=principal)
                   for name, needed in requirements.items() if bool(needed)}
        states = {r["state"] for r in results.values()}
        allowed = bool(results) and states == {"supported"}
        aggregate = ("supported" if allowed else "conflicting" if "conflicting" in states
                     or ({"supported", "unsupported"} <= states) else "unsupported"
                     if "unsupported" in states else "unknown")
        return {"allowed": allowed, "state": aggregate,
                "requirements": results, "fail_closed": not allowed,
                "registry_version": self.policy["registry_version"]}

    # A name is deliberately just another exact subject key.
    assess = assess_requirements

    def validate_multimodal_request(self, *, environment_id: str, model_id: str,
                                    channel_id: str, subject_version: str,
                                    request_type: str, mime_type: str,
                                    input_bytes: int,
                                    principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Validate media metadata against reviewed capability evidence.

        The method never stores the filename or media bytes and never performs a
        provider request. A bounded audit record makes successful and blocked
        validations observable and repeatable.
        """
        scope = self._authorize(principal, "evidence.read")
        requirement_by_type = {"image": "image_in", "audio": "audio", "video": "video_in"}
        requirement = requirement_by_type.get(str(request_type).strip().casefold())
        if requirement is None:
            raise CapabilityEvidenceError("unsupported_multimodal_request_type")
        if (not environment_id or not model_id or not channel_id or not subject_version
                or not mime_type or not isinstance(input_bytes, int)
                or isinstance(input_bytes, bool) or input_bytes <= 0):
            raise CapabilityEvidenceError("invalid_multimodal_request_metadata")
        maximum = int(self.policy["media_bounds"]["maximum_input_bytes"])
        if input_bytes > maximum:
            raise CapabilityEvidenceError("multimodal_input_exceeds_global_limit")
        subject_id = f"model:{model_id}|channel:{channel_id}"
        resolved = self.resolve(
            environment_id=environment_id, subject_id=subject_id,
            subject_version=subject_version, requirement=requirement,
            decision_source="live", request_constraints={
                "mime_type": mime_type.casefold(), "input_bytes": input_bytes,
            }, principal=principal)
        validation_id = f"MMV-{uuid.uuid4().hex.upper()}"
        state = "validated" if resolved["supported"] else "blocked"
        reason = "capability_confirmed" if resolved["supported"] else (
            resolved["rejected_evidence"][0]["reason"]
            if resolved["rejected_evidence"] else "capability_pending_confirmation")
        created_at = _iso(self.clock())
        with self._connection() as db:
            db.execute("""INSERT INTO multimodal_validation_runs(
              validation_id,environment_id,model_id,channel_id,request_type,mime_type,
              input_bytes,state,reason,evidence_ids_json,created_at,tenant_id,workspace_id)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                validation_id, environment_id, model_id, channel_id, request_type,
                mime_type.casefold(), input_bytes, state, reason,
                json.dumps(resolved["evidence_ids"]), created_at,
                *scope.sql_parameters()))
            db.execute("""INSERT INTO capability_evidence_audit(
              event_type,created_at,details_json,tenant_id,workspace_id) VALUES(?,?,?,?,?)""", (
                "multimodal_request_validated", created_at,
                json.dumps({"validation_id": validation_id, "state": state,
                            "reason": reason, "request_type": request_type}),
                *scope.sql_parameters()))
        return {
            "validation_id": validation_id, "status": state, "allowed": resolved["supported"],
            "reason": reason, "request_type": request_type, "mime_type": mime_type.casefold(),
            "input_bytes": input_bytes, "model_id": model_id, "channel_id": channel_id,
            "evidence_ids": resolved["evidence_ids"],
            "registry_version": resolved["registry_version"],
            "network_called": False, "media_persisted": False,
        }

    def list_evidence(self, *, subject_id: str | None = None, limit: int = 100,
                      principal: PrincipalContext | None = None) -> list[dict[str, Any]]:
        scope = self._authorize(principal, "evidence.read")
        if not 1 <= limit <= 1000: raise CapabilityEvidenceError("invalid_evidence_limit")
        query = "SELECT * FROM capability_evidence WHERE tenant_id=? AND workspace_id=?"; params: list[Any] = list(scope.sql_parameters())
        if subject_id is not None: query += " AND subject_id=?"; params.append(subject_id)
        query += " ORDER BY observed_at DESC,evidence_id LIMIT ?"; params.append(limit)
        with self._connection() as db: rows = db.execute(query, params).fetchall()
        return [{**dict(row), "details": _redact_details(json.loads(row["details_json"])),
                 "mime_types": json.loads(row["mime_types_json"])} for row in rows]

    def require_validated_multimodal(self, *, validation_id: str,
                                     environment_id: str, model_id: str,
                                     channel_id: str, request_type: str,
                                     principal: PrincipalContext | None = None) -> dict[str, Any]:
        """Bind a real execution to a previously accepted, tenant-scoped validation."""
        scope = self._authorize(principal, "evidence.read")
        if not validation_id:
            raise CapabilityEvidenceError("multimodal_validation_required")
        with self._connection() as db:
            row = db.execute("""SELECT validation_id,environment_id,model_id,channel_id,
              request_type,state,reason FROM multimodal_validation_runs
              WHERE tenant_id=? AND workspace_id=? AND validation_id=?""",
              (*scope.sql_parameters(), validation_id)).fetchone()
        if row is None:
            raise CapabilityEvidenceError("multimodal_validation_not_found")
        expected = (environment_id, model_id, channel_id, request_type)
        observed = (row["environment_id"], row["model_id"], row["channel_id"], row["request_type"])
        if observed != expected:
            raise CapabilityEvidenceError("multimodal_validation_scope_mismatch")
        if row["state"] != "validated":
            raise CapabilityEvidenceError("multimodal_capability_pending")
        return dict(row)

    def monitoring_status(self, *, subject_id: str | None = None, limit: int = 100,
                          principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "evidence.read")
        if not 1 <= limit <= 500: raise CapabilityEvidenceError("invalid_evidence_limit")
        now = self.clock(); query = """SELECT evidence_id,evidence_type,observed_at,environment_id,
          subject_id,subject_version,source,requirement,state,registry_version,scenario_id,
          mime_types_json,maximum_input_bytes,maximum_output_bytes,maximum_context_tokens,
          valid_until,revoked_at,supersedes_evidence_id FROM capability_evidence
          WHERE tenant_id=? AND workspace_id=?"""
        args: list[Any] = list(scope.sql_parameters())
        if subject_id is not None: query += " AND subject_id=?"; args.append(subject_id)
        query += " ORDER BY observed_at DESC,evidence_id LIMIT ?"; args.append(limit)
        with self._connection() as db: rows = db.execute(query, args).fetchall()
        items = []
        for row in rows:
            item = dict(row); item["mime_types"] = json.loads(item.pop("mime_types_json"))
            stale = (now - _parse(row["observed_at"])).total_seconds() > self.policy["maximum_evidence_age_seconds"]
            expired = bool(row["valid_until"] and _parse(row["valid_until"]) <= now)
            item["verification_status"] = ("revoked" if row["revoked_at"] else "stale"
                if stale else "expired" if expired else "pending_confirmation"
                if row["state"] == "unknown" or row["evidence_type"] == "legacy_assumption" else row["state"])
            items.append(item)
        return {"status": "ready" if items else "pending_confirmation",
                "policy_version": self.policy["policy_version"], "registry_version": self.policy["registry_version"],
                "fixed_scenarios": self.policy["fixed_scenarios"], "media_bounds": self.policy["media_bounds"],
                "items": items, "network_called": False}

    def monitoring_audit(self, limit: int = 100, *,
                         principal: PrincipalContext | None = None) -> dict[str, Any]:
        scope = self._authorize(principal, "audit.read")
        if not 1 <= limit <= 500: raise CapabilityEvidenceError("invalid_audit_limit")
        with self._connection() as db:
            rows = db.execute("""SELECT audit_id,event_type,created_at FROM capability_evidence_audit
                              WHERE tenant_id=? AND workspace_id=? ORDER BY audit_id DESC LIMIT ?""",
                              (*scope.sql_parameters(), limit)).fetchall()
        return {"status": "ready", "items": [dict(row) for row in rows], "network_called": False}
