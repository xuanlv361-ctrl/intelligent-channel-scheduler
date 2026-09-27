"""Append-only JSON Lines decision audit logger."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Mapping


SENSITIVE_TERMS = (
    "api_key", "apikey", "authorization", "secret",
    "cookie", "password", "credential", "dpapi", "browser_storage",
    "storage_state", "local_storage", "session_storage", "prompt", "messages",
    "raw_request", "raw_response", "request_body", "response_body",
)
SENSITIVE_TOKEN_KEYS = frozenset({
    "token", "access_token", "refresh_token", "id_token", "bearer_token",
    "auth_token", "session_token", "csrf_token",
})
PROTECTED_KEYS = frozenset({"body", "content", "response"})


def canonical_json(value: Any) -> str:
    """Return the stable JSON representation used by reconstruction hashes."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest().upper()


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold() in PROTECTED_KEYS
                or str(key).casefold() in SENSITIVE_TOKEN_KEYS
                or any(term in str(key).casefold() for term in SENSITIVE_TERMS)
                else redact_sensitive(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


class DecisionLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def log(self, decision: Mapping[str, Any]) -> None:
        required = {"decision_id", "request_id", "outcome", "is_mock"}
        missing = sorted(required - set(decision))
        if missing:
            raise ValueError(f"decision log missing fields: {', '.join(missing)}")
        if decision["is_mock"] is not True:
            raise ValueError("Scheduler MVP decisions must be marked is_mock=true")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(
                redact_sensitive(dict(decision)),
                ensure_ascii=False,
                sort_keys=True,
            ))
            handle.write("\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def log_runtime(self, decision: Mapping[str, Any]) -> None:
        required = {"decision_id", "runtime_version", "request_id", "mode", "strategy"}
        missing = sorted(required - set(decision))
        if missing:
            raise ValueError(f"runtime decision log missing fields: {', '.join(missing)}")
        safe = redact_sensitive(dict(decision))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + "\n")

    def find_by_decision_id(self, decision_id: str) -> dict[str, Any] | None:
        return next((row for row in self.read_all() if row.get("decision_id") == decision_id), None)

    def find_unique_by_decision_id(self, decision_id: str) -> dict[str, Any] | None:
        """Return one immutable decision or fail closed on conflicting reuse."""

        matches = [row for row in self.read_all() if row.get("decision_id") == decision_id]
        if not matches:
            return None
        fingerprints = {content_sha256(row) for row in matches}
        if len(fingerprints) != 1:
            raise ValueError("decision_id_conflict")
        return matches[0]

    def find_by_request_id(self, request_id: str) -> list[dict[str, Any]]:
        return [row for row in self.read_all() if row.get("request_id") == request_id]
