from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.security.authorization import AuthorizationService
from backend.security.config import EnterpriseIdentityConfig
from backend.security.csrf import CSRFError, CSRFProtector
from backend.security.identity import PrincipalResolver, WindowsDevelopmentIdentityProvider
from backend.security.session import EnterpriseSessionManager


ROOT = Path(__file__).resolve().parents[1]
SESSION_KEY = b"csrf-session-signing-material-is-injected-001"
CSRF_KEY = b"csrf-token-signing-material-is-injected-00001"


class Clock:
    def __init__(self): self.value = datetime(2026, 8, 2, tzinfo=timezone.utc)
    def __call__(self): return self.value
    def advance(self, seconds): self.value += timedelta(seconds=seconds)


def setup(tmp_path, clock):
    config = EnterpriseIdentityConfig.load(ROOT / "config/enterprise_identity_v1.json")
    authz = AuthorizationService(ROOT / "config/rbac_permissions_v1.json", clock=clock)
    resolver = PrincipalResolver(config, authz)
    principal = resolver.resolve(WindowsDevelopmentIdentityProvider(
        config, username_loader=lambda: "Alice", clock=clock))
    sessions = EnterpriseSessionManager(tmp_path / "csrf.sqlite3", signing_key=SESSION_KEY,
        resolver=resolver, absolute_ttl_seconds=100, idle_ttl_seconds=50, clock=clock)
    raw, validation = sessions.create(principal)
    return sessions, CSRFProtector(CSRF_KEY, sessions, ttl_seconds=10, clock=clock), raw, validation


def test_valid_csrf_and_safe_method_boundary(tmp_path):
    clock = Clock(); _, csrf, _, session = setup(tmp_path, clock)
    token = csrf.issue(session)
    csrf.verify(token, session, method="POST", origin="https://console.example",
                allowed_origins={"https://console.example"})
    csrf.verify(None, session, method="GET", origin=None, allowed_origins=set())


@pytest.mark.parametrize("token_mode,origin,error", [
    ("missing", "https://console.example", "csrf_token_missing_or_invalid"),
    ("valid", None, "csrf_origin_rejected"),
    ("valid", "https://evil.example", "csrf_origin_rejected"),
])
def test_missing_token_and_origin_rejected(tmp_path, token_mode, origin, error):
    clock = Clock(); _, csrf, _, session = setup(tmp_path, clock)
    token = None if token_mode == "missing" else csrf.issue(session)
    with pytest.raises(CSRFError, match=error):
        csrf.verify(token, session, method="POST", origin=origin,
                    allowed_origins={"https://console.example"})


def test_other_session_and_replay_are_rejected(tmp_path):
    clock = Clock(); sessions, csrf, raw, first = setup(tmp_path, clock)
    token = csrf.issue(first)
    _, second = sessions.create(first.principal)
    with pytest.raises(CSRFError, match="csrf_session_scope_mismatch"):
        csrf.verify(token, second, method="POST", origin="https://console.example",
                    allowed_origins={"https://console.example"})
    csrf.verify(token, first, method="POST", origin="https://console.example",
                allowed_origins={"https://console.example"})
    with pytest.raises(CSRFError, match="csrf_token_replay_detected"):
        csrf.verify(token, first, method="POST", origin="https://console.example",
                    allowed_origins={"https://console.example"})
    assert raw  # session cookie material remains separate from CSRF token


def test_expired_and_rotated_csrf_tokens_are_rejected(tmp_path):
    clock = Clock(); sessions, csrf, raw, session = setup(tmp_path, clock)
    expired = csrf.issue(session); clock.advance(11)
    with pytest.raises(CSRFError, match="csrf_token_expired"):
        csrf.verify(expired, session, method="POST", origin="https://console.example",
                    allowed_origins={"https://console.example"})
    current = sessions.validate(raw, touch=False)
    old = csrf.issue(current)
    sessions.rotate_csrf(current.session_id)
    rotated = sessions.validate(raw, touch=False)
    with pytest.raises(CSRFError, match="csrf_session_scope_mismatch"):
        csrf.verify(old, rotated, method="POST", origin="https://console.example",
                    allowed_origins={"https://console.example"})

