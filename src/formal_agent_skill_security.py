"""Short-lived one-use authorization for formal read-only Skills."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from formal_agent_skill_registry import FormalAgentSkillError


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SkillGrant:
    grant_id: str
    session_id: str
    lease_id: str
    actor_id: str
    origin: str
    skill_id: str
    binding: str
    operation: str
    environment_id: str
    precondition_sha256: str
    budget_policy_version: str
    circuit_policy_version: str
    capability_evidence_version: str
    kill_switch_version: str
    expires_at: str
    seal: str


def repository_harness_preconditions(_context: Mapping[str, str]) -> dict[str, Any]:
    """Explicit local-only defaults; they authorize no network or mutation."""
    return {
        "budget": {"enabled": True, "allowed": True,
                   "version": "formal-skill-repository-read-budget-v1"},
        "circuit": {"enabled": True, "state": "CLOSED",
                    "version": "formal-skill-repository-circuit-v1"},
        "capability": {"enabled": True, "allowed": True, "status": "confirmed",
                       "version": "formal-skill-repository-capability-v1"},
        "kill_switch": {"enforcement_enabled": True, "active": False,
                        "version": "formal-skill-repository-kill-switch-v1"},
    }


class FormalAgentSkillSecurity:
    def __init__(self, *, allowed_origins: set[str], allowed_skill_ids: set[str],
                 clock: Callable[[], datetime] = _now, grant_ttl_seconds: int = 20,
                 session_ttl_seconds: int = 120,
                 precondition_provider: Callable[[Mapping[str, str]], Mapping[str, Any]]
                 = repository_harness_preconditions):
        self.allowed_origins = frozenset(allowed_origins)
        self.allowed_skill_ids = frozenset(allowed_skill_ids)
        self.clock = clock
        self.grant_ttl = min(max(int(grant_ttl_seconds), 1), 30)
        self.session_ttl = min(max(int(session_ttl_seconds), 1), 300)
        if not callable(precondition_provider):
            raise FormalAgentSkillError("formal_skill_precondition_provider_invalid")
        self.precondition_provider = precondition_provider
        self._secret = secrets.token_bytes(32)
        self._sessions: dict[str, dict] = {}
        self._leases: dict[str, dict] = {}
        self._grants: dict[str, SkillGrant] = {}
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise FormalAgentSkillError("formal_skill_clock_invalid")
        return value.astimezone(timezone.utc)

    def open_session(self, *, actor_id: str, origin: str) -> str:
        if origin not in self.allowed_origins:
            raise FormalAgentSkillError("formal_skill_origin_rejected")
        if not actor_id or len(actor_id) > 128:
            raise FormalAgentSkillError("formal_skill_actor_invalid")
        session_id = f"FAS-{uuid.uuid4()}"
        with self._lock:
            self._sessions[session_id] = {
                "actor_id": actor_id, "origin": origin,
                "expires_at": self._now() + timedelta(seconds=self.session_ttl),
            }
        return session_id

    def acquire_lease(self, *, session_id: str, skill_id: str,
                      environment_id: str = "local") -> str:
        with self._lock:
            session = self._sessions.get(session_id)
            if not session or session["expires_at"] <= self._now():
                raise FormalAgentSkillError("formal_skill_session_invalid_or_expired")
            if skill_id not in self.allowed_skill_ids:
                raise FormalAgentSkillError("formal_skill_not_registered")
            lease_id = f"FAL-{uuid.uuid4()}"
            self._leases[lease_id] = {
                "session_id": session_id, "skill_id": skill_id,
                "environment_id": environment_id,
                "expires_at": min(session["expires_at"], self._now() + timedelta(seconds=60)),
            }
        return lease_id

    def _seal(self, claims: dict) -> str:
        raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._secret, raw, hashlib.sha256).hexdigest()

    @staticmethod
    def _version(value: Any, gate: str) -> str:
        version = str(value or "").strip()
        if not version or len(version) > 128:
            raise FormalAgentSkillError(
                f"formal_skill_{gate}_precondition_version_missing")
        return version

    def _precondition_snapshot(self, *, skill_id: str, binding: str,
                               environment_id: str) -> dict[str, str]:
        context = {"skill_id": skill_id, "binding": binding,
                   "environment_id": environment_id, "operation": "read"}
        try:
            supplied = self.precondition_provider(context)
        except FormalAgentSkillError:
            raise
        except Exception as exc:
            raise FormalAgentSkillError(
                "formal_skill_precondition_provider_unavailable") from exc
        if not isinstance(supplied, Mapping):
            raise FormalAgentSkillError("formal_skill_preconditions_missing")
        required = ("budget", "circuit", "capability", "kill_switch")
        if any(not isinstance(supplied.get(name), Mapping) for name in required):
            raise FormalAgentSkillError("formal_skill_preconditions_missing")
        budget = supplied["budget"]
        circuit = supplied["circuit"]
        capability = supplied["capability"]
        kill_switch = supplied["kill_switch"]
        if budget.get("enabled") is not True or budget.get("allowed") is not True:
            raise FormalAgentSkillError("formal_skill_budget_precondition_denied")
        if circuit.get("enabled") is not True or circuit.get("state") != "CLOSED":
            raise FormalAgentSkillError("formal_skill_circuit_precondition_denied")
        if (capability.get("enabled") is not True
                or capability.get("allowed") is not True
                or capability.get("status") not in {"confirmed", "supported"}):
            raise FormalAgentSkillError("formal_skill_capability_precondition_denied")
        if (kill_switch.get("enforcement_enabled") is not True
                or kill_switch.get("active") is not False):
            raise FormalAgentSkillError("formal_skill_kill_switch_precondition_denied")
        versions = {
            "budget_policy_version": self._version(budget.get("version"), "budget"),
            "circuit_policy_version": self._version(circuit.get("version"), "circuit"),
            "capability_evidence_version": self._version(
                capability.get("version"), "capability"),
            "kill_switch_version": self._version(
                kill_switch.get("version"), "kill_switch"),
        }
        canonical = {
            "context": context,
            "budget": {"enabled": True, "allowed": True,
                       "version": versions["budget_policy_version"]},
            "circuit": {"enabled": True, "state": "CLOSED",
                        "version": versions["circuit_policy_version"]},
            "capability": {"enabled": True, "allowed": True,
                           "status": str(capability["status"]),
                           "version": versions["capability_evidence_version"]},
            "kill_switch": {"enforcement_enabled": True, "active": False,
                            "version": versions["kill_switch_version"]},
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        return {**versions, "precondition_sha256": hashlib.sha256(raw).hexdigest()}

    def issue_grant(self, *, session_id: str, lease_id: str, skill_id: str,
                    binding: str, operation: str = "read") -> SkillGrant:
        from execution_guard import ExecutionGuard
        now = self._now()
        with self._lock:
            session, lease = self._sessions.get(session_id), self._leases.get(lease_id)
        if not session or session["expires_at"] <= now:
            raise FormalAgentSkillError("formal_skill_session_invalid_or_expired")
        if (not lease or lease["expires_at"] <= now or lease["session_id"] != session_id
                or lease["skill_id"] != skill_id):
            raise FormalAgentSkillError("formal_skill_lease_invalid_or_expired")
        if operation != "read" or not binding.endswith(".read"):
            raise FormalAgentSkillError("formal_skill_operation_forbidden")
        preconditions = self._precondition_snapshot(
            skill_id=skill_id, binding=binding,
            environment_id=str(lease["environment_id"]))
        guard = ExecutionGuard.check_formal_skill({
            "registered": skill_id in self.allowed_skill_ids, "read_only": True,
            "operation_allowed": operation == "read",
            "origin_allowed": session["origin"] in self.allowed_origins,
            "session_active": session["expires_at"] > now,
            "lease_active": lease["expires_at"] > now,
            "binding_static": binding.endswith(".read"), "network_disabled": True,
            "path_safe": True, "bypass_absent": True,
        })
        if guard.status != "allowed":
            raise FormalAgentSkillError(guard.error_category or "formal_skill_guard_denied")
        claims = {
            "grant_id": f"FAG-{uuid.uuid4()}", "session_id": session_id,
            "lease_id": lease_id, "actor_id": session["actor_id"],
            "origin": session["origin"], "skill_id": skill_id, "binding": binding,
            "operation": operation, "environment_id": lease["environment_id"],
            **preconditions,
            "expires_at": min(lease["expires_at"], now + timedelta(seconds=self.grant_ttl)).isoformat(),
        }
        grant = SkillGrant(**claims, seal=self._seal(claims))
        with self._lock:
            self._grants[grant.grant_id] = grant
        return grant

    def validate_grant(self, grant: SkillGrant, *, skill_id: str, binding: str) -> dict:
        if not isinstance(grant, SkillGrant):
            raise FormalAgentSkillError("formal_skill_grant_required")
        with self._lock:
            stored = self._grants.get(grant.grant_id)
        claims = asdict(grant); seal = claims.pop("seal")
        if (stored != grant or not hmac.compare_digest(seal, self._seal(claims))
                or grant.skill_id != skill_id or grant.binding != binding
                or grant.operation != "read" or grant.origin not in self.allowed_origins
                or datetime.fromisoformat(grant.expires_at) <= self._now()):
            raise FormalAgentSkillError("formal_skill_grant_invalid_expired_or_reused")
        lease = self._leases.get(grant.lease_id)
        if not lease or lease["session_id"] != grant.session_id or lease["skill_id"] != skill_id:
            raise FormalAgentSkillError("formal_skill_lease_invalid_or_expired")
        try:
            current = self._precondition_snapshot(
                skill_id=skill_id, binding=binding,
                environment_id=grant.environment_id)
        except Exception:
            with self._lock:
                self._grants.pop(grant.grant_id, None)
            raise
        expected = {
            key: claims[key] for key in (
                "precondition_sha256", "budget_policy_version",
                "circuit_policy_version", "capability_evidence_version",
                "kill_switch_version")
        }
        if current != expected:
            with self._lock:
                self._grants.pop(grant.grant_id, None)
            raise FormalAgentSkillError("formal_skill_precondition_state_drift")
        return claims

    def consume_grant(self, grant: SkillGrant, *, skill_id: str, binding: str) -> dict:
        with self._lock:
            claims = self.validate_grant(grant, skill_id=skill_id, binding=binding)
            self._grants.pop(grant.grant_id, None)
            return claims
