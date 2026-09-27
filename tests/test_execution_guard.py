import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from execution_guard import ExecutionGuard  # noqa: E402
from channel_adapter import MockChannelAdapter  # noqa: E402

CONFIG = json.loads((ROOT / "config" / "scheduler_runtime_v1.json").read_text(encoding="utf-8"))


def test_decision_only_modes_never_execute():
    guard = ExecutionGuard(CONFIG)
    for mode in ("simulation", "real_shadow"):
        result = guard.check(mode, None, None)
        assert not result.routing_allowed and not result.execution_attempted


def test_mock_execute_requires_mock_candidate_and_adapter():
    guard = ExecutionGuard(CONFIG)
    assert guard.check("mock_execute", {"is_mock": "TRUE"}, MockChannelAdapter()).status == "allowed"
    assert guard.check("mock_execute", {"is_mock": "FALSE"}, MockChannelAdapter()).status == "blocked"
    assert guard.check("mock_execute", {"is_mock": "TRUE"}, None).status == "blocked"


def test_real_execute_is_unconditionally_blocked():
    result = ExecutionGuard(CONFIG).check("real_execute", {"is_mock": "FALSE", "routing_eligible": "TRUE"}, object())
    assert result.status == "blocked" and result.error_category == "real_execution_not_authorized"


def test_missing_or_unsafe_config_fails_closed():
    assert ExecutionGuard({}).check("simulation", None).status == "blocked"
    changed = copy.deepcopy(CONFIG)
    changed["max_attempts"] = 3
    assert ExecutionGuard(changed).check("mock_execute", {"is_mock": "TRUE"}, MockChannelAdapter()).status == "blocked"
    changed = copy.deepcopy(CONFIG)
    changed["fallback_enabled"] = False
    assert ExecutionGuard(changed).check("mock_execute", {"is_mock": "TRUE"}, MockChannelAdapter()).status == "blocked"


def test_unsafe_config_cannot_reenable_network_regardless_of_attempts():
    for field in ("network_access_enabled", "real_api_adapter_enabled", "real_execution_enabled"):
        changed = copy.deepcopy(CONFIG)
        changed[field] = True
        result = ExecutionGuard(changed).check("mock_execute", {"is_mock": "TRUE"}, MockChannelAdapter())
        assert result.status == "blocked" and result.error_category == "network_access_must_remain_disabled"


def test_bounded_fallback_within_safety_ceiling_is_allowed():
    result = ExecutionGuard(CONFIG).check("mock_execute", {"is_mock": "TRUE"}, MockChannelAdapter())
    assert result.status == "allowed"
    assert CONFIG["max_attempts"] == 2 and CONFIG["fallback_enabled"] is True


def test_unknown_mode_fails_closed():
    assert ExecutionGuard(CONFIG).check("unknown", None).error_category == "unknown_runtime_mode"
