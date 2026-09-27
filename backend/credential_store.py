"""Process-memory, per-browser UAT credentials. Secrets are never serialized."""
from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:8]


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


@dataclass
class _Credential:
    value: str
    environment_id: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime

    def __repr__(self) -> str:
        return f"_Credential(value=<redacted>, environment_id={self.environment_id!r}, created_at={self.created_at!r}, expires_at={self.expires_at!r})"


class SessionCredentialStore:
    def __init__(self, ttl_minutes: int = 30, max_sessions: int = 20):
        self.ttl_minutes = max(1, ttl_minutes)
        self.max_sessions = max(1, max_sessions)
        self._items: dict[tuple[str, str], _Credential] = {}
        self._lock = threading.RLock()

    def _cleanup(self, when: datetime) -> list[str]:
        expired = [key for key, item in self._items.items() if item.expires_at <= when]
        for key in expired:
            del self._items[key]
        return expired

    def create(self, value: str, when: datetime | None = None, environment_id: str = "china_uat",
               session_id: str | None = None) -> tuple[str, dict]:
        when = when or utcnow()
        with self._lock:
            self._cleanup(when)
            active_sessions = {sid for sid, _ in self._items}
            if session_id is None and len(active_sessions) >= self.max_sessions:
                raise OverflowError("maximum_active_credential_sessions_reached")
            sid = session_id or secrets.token_urlsafe(32)
            expires = when + timedelta(minutes=self.ttl_minutes)
            self._items[(sid, environment_id)] = _Credential(value, environment_id, when, expires, when)
            return sid, self.metadata(sid, when, environment_id) or {}

    def resolve(self, session_id: str | None, when: datetime | None = None,
                environment_id: str = "china_uat") -> tuple[str | None, dict | None]:
        when = when or utcnow()
        if not session_id:
            return None, None
        with self._lock:
            self._cleanup(when)
            item = self._items.get((session_id, environment_id))
            if not item:
                return None, None
            item.last_used_at = when
            return item.value, self._metadata(item, when)

    def metadata(self, session_id: str | None, when: datetime | None = None,
                 environment_id: str = "china_uat") -> dict | None:
        when = when or utcnow()
        if not session_id:
            return None
        with self._lock:
            self._cleanup(when)
            item = self._items.get((session_id, environment_id))
            return self._metadata(item, when) if item else None

    def clear(self, session_id: str | None, environment_id: str = "china_uat") -> bool:
        if not session_id:
            return False
        with self._lock:
            return self._items.pop((session_id, environment_id), None) is not None

    def _metadata(self, item: _Credential, when: datetime) -> dict:
        return {
            "created_at": item.created_at.isoformat(),
            "expires_at": item.expires_at.isoformat(),
            "last_used_at": item.last_used_at.isoformat(),
            "minutes_remaining": max(0, int((item.expires_at - when).total_seconds() // 60)),
            "key_fingerprint": fingerprint(item.value),
            "credential_source": "session",
            "environment_id": item.environment_id,
        }
