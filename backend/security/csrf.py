from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from .session import EnterpriseSessionManager, SessionError, SessionValidation


class CSRFError(PermissionError):
    pass


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class CSRFProtector:
    def __init__(self, signing_key: bytes, sessions: EnterpriseSessionManager, *,
                 ttl_seconds: int = 900,
                 clock: Callable[[], datetime] | None = None):
        if len(signing_key) < 32 or ttl_seconds <= 0:
            raise CSRFError("csrf_policy_invalid")
        self.key, self.sessions, self.ttl_seconds = bytes(signing_key), sessions, int(ttl_seconds)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def issue(self, session: SessionValidation) -> str:
        now = self.clock().astimezone(timezone.utc)
        payload = {
            "sid": session.session_id, "jti": session.jti,
            "tenant": session.principal.tenant_id,
            "workspace": session.principal.workspace_id,
            "generation": session.csrf_generation,
            "nonce": secrets.token_urlsafe(24),
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=self.ttl_seconds)).timestamp()),
        }
        encoded = _b64(json.dumps(payload, sort_keys=True,
                                  separators=(",", ":")).encode())
        signature = _b64(hmac.new(self.key, encoded.encode(), hashlib.sha256).digest())
        return f"csrf1.{encoded}.{signature}"

    def verify(self, token: str | None, session: SessionValidation, *, method: str,
               origin: str | None, allowed_origins: Iterable[str],
               consume: bool = True) -> None:
        if method.upper() in SAFE_METHODS:
            return
        if origin is None or origin not in frozenset(allowed_origins):
            raise CSRFError("csrf_origin_rejected")
        parts = str(token or "").split(".")
        if len(parts) != 3 or parts[0] != "csrf1":
            raise CSRFError("csrf_token_missing_or_invalid")
        expected = _b64(hmac.new(self.key, parts[1].encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, parts[2]):
            raise CSRFError("csrf_token_invalid")
        try:
            payload = json.loads(_unb64(parts[1]))
        except Exception as exc:
            raise CSRFError("csrf_token_invalid") from exc
        now = int(self.clock().astimezone(timezone.utc).timestamp())
        expected_fields = {
            "sid": session.session_id, "jti": session.jti,
            "tenant": session.principal.tenant_id,
            "workspace": session.principal.workspace_id,
            "generation": session.csrf_generation,
        }
        if any(payload.get(key) != value for key, value in expected_fields.items()):
            raise CSRFError("csrf_session_scope_mismatch")
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= now:
            raise CSRFError("csrf_token_expired")
        nonce = payload.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise CSRFError("csrf_token_invalid")
        if consume:
            try:
                self.sessions.consume_nonce(session.session_id, nonce, "csrf")
            except SessionError as exc:
                if str(exc) == "session_replay_detected":
                    raise CSRFError("csrf_token_replay_detected") from exc
                raise CSRFError("csrf_replay_state_unavailable") from exc
