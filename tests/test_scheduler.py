import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_logger import DecisionLogger  # noqa: E402
from request_router import Request, RequestRouter, RequestValidationError  # noqa: E402
from scheduler import Scheduler  # noqa: E402


REQUEST = {
    "request_id": "REQ001",
    "requested_model": "deepseek-v4-flash",
    "stream_required": False,
    "input_tokens": 1000,
    "output_tokens": 500,
    "currency": "CNY",
    "strategy": "confidence_aware_v2",
}
FIXED_CLOCK = lambda: "2026-07-21 12:00:00"


def make_scheduler(tmp_path, **kwargs):
    return Scheduler(logger=DecisionLogger(tmp_path / "decisions.jsonl"), clock=FIXED_CLOCK, **kwargs)


def test_request_router_accepts_contract_and_returns_immutable_request():
    request = RequestRouter({"confidence_aware_v2"}).route(REQUEST)
    assert isinstance(request, Request)
    assert request.to_dict() == REQUEST


@pytest.mark.parametrize(
    "patch,match",
    [
        ({"request_id": ""}, "request_id"),
        ({"stream_required": "false"}, "stream_required"),
        ({"input_tokens": -1}, "input_tokens"),
        ({"output_tokens": 1.5}, "output_tokens"),
        ({"strategy": "unknown"}, "unsupported strategy"),
    ],
)
def test_request_validation_errors(patch, match):
    with pytest.raises(RequestValidationError, match=match):
        RequestRouter({"confidence_aware_v2"}).route({**REQUEST, **patch})


def test_request_validation_reports_missing_field():
    payload = dict(REQUEST)
    del payload["currency"]
    with pytest.raises(RequestValidationError, match="currency"):
        RequestRouter({"confidence_aware_v2"}).route(payload)


def test_scheduler_runs_complete_mvp_chain_and_logs(tmp_path):
    scheduler = make_scheduler(tmp_path)
    result = scheduler.schedule(REQUEST)
    assert result["outcome"] == "selected"
    assert result["selected_candidate"] == "MOCK-DS-FAST"
    assert result["strategy"] == "confidence_aware_v2"
    assert result["is_mock"] is True and result["real_api_called"] is False
    assert result["adapter_receipt"]["status"] == "mock_accepted"
    assert scheduler.logger.read_all() == [result]


def test_unroutable_request_is_logged_and_adapter_not_called(tmp_path):
    result = make_scheduler(tmp_path).schedule({**REQUEST, "request_id": "NOPE", "requested_model": "missing-model"})
    assert result["outcome"] == "unroutable"
    assert result["selected_candidate"] is None
    assert result["adapter_receipt"] is None


def test_round_robin_uses_scheduler_request_index(tmp_path):
    scheduler = make_scheduler(tmp_path)
    payload = {**REQUEST, "strategy": "round_robin"}
    assert scheduler.schedule(payload)["selected_candidate"] == "REAL-DS-48"
    assert scheduler.schedule({**payload, "request_id": "REQ002"})["selected_candidate"] == "MOCK-DS-FAST"


def test_invalid_request_is_neither_dispatched_nor_logged(tmp_path):
    scheduler = make_scheduler(tmp_path)
    with pytest.raises(RequestValidationError):
        scheduler.schedule({**REQUEST, "input_tokens": -1})
    assert scheduler.logger.read_all() == []


def test_fixed_configuration_produces_repeatable_decision(tmp_path):
    first = make_scheduler(tmp_path / "a").schedule(REQUEST)
    second = make_scheduler(tmp_path / "b").schedule(REQUEST)
    assert first == second


def test_scheduler_does_not_modify_strategy_inputs_or_protected_evidence(tmp_path):
    paths = [
        ROOT / "data" / "candidate_channels_v1.csv",
        ROOT / "config" / "strategy_catalog_v2.json",
        ROOT / "config" / "decision_policy_v1.json",
        ROOT / "data" / "metrics_snapshot_v1.csv",
        ROOT / "data" / "model_channel_matrix.csv",
        ROOT / "data" / "request_records.csv",
        ROOT / "data" / "first_week_source_records.csv",
    ]
    digest = lambda path: hashlib.sha256(path.read_bytes()).digest()
    before = {path: digest(path) for path in paths}
    make_scheduler(tmp_path).schedule(REQUEST)
    assert before == {path: digest(path) for path in paths}


def test_demo_runs_without_network_and_emits_mock_result(tmp_path):
    demo = ROOT / "examples" / "run_scheduler_demo.py"
    log_path = tmp_path / "demo.jsonl"
    result = subprocess.run(
        [sys.executable, str(demo), "--log-path", str(log_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["is_mock"] is True and payload["real_api_called"] is False
    assert log_path.exists()


def test_new_runtime_sources_have_no_network_clients():
    for name in ("request_router.py", "scheduler.py", "channel_adapter.py", "decision_logger.py"):
        source = (ROOT / "src" / name).read_text(encoding="utf-8")
        assert all(token not in source for token in ("requests", "urllib", "http.client", "socket", "aiohttp"))
