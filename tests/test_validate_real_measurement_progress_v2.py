import ast
import csv
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import record_real_measurement_v2 as recorder  # noqa: E402
import validate_real_measurement_progress_v2 as validator  # noqa: E402


def workspace(base: Path):
    plan = base / "plan.csv"
    payload = base / "payload.json"
    profiles = base / "profiles.csv"
    shutil.copyfile(recorder.DEFAULT_PLAN_PATH, plan)
    shutil.copyfile(recorder.DEFAULT_PAYLOAD_PATH, payload)
    shutil.copyfile(ROOT / "data" / "request_profiles_v2.csv", profiles)
    evidence = base / "evidence"
    evidence.mkdir()
    (evidence / "test.png").write_bytes(b"test")
    (evidence / "log.png").write_bytes(b"log")
    return {
        "plan": plan, "payload": payload, "profiles": profiles,
        "results": base / "results.csv", "backups": base / "backups",
        "evidence": evidence,
    }


def observed(paths, **changes):
    value = {
        "measured_at": "2026-07-23 10:30:00", "result": "success",
        "latency_ms": "800", "ttft_ms": "", "input_tokens": "20",
        "output_tokens": "60", "total_tokens": "80", "cost_cny": "0.00014",
        "http_status": "200", "actual_model": "deepseek-v4-flash",
        "request_id": "observed-id", "finish_reason": "stop",
        "sse_complete": "", "done_received": "", "error_category": "",
        "evidence_test_path": str(paths["evidence"] / "test.png"),
        "evidence_log_path": str(paths["evidence"] / "log.png"), "notes": "",
    }
    value.update(changes)
    return value


def save(paths, plan_id, value):
    return recorder.record_result(
        plan_path=paths["plan"], payload_path=paths["payload"],
        profiles_path=paths["profiles"], results_path=paths["results"],
        backup_dir=paths["backups"], plan_id=plan_id, actual=value,
        evidence_base=paths["evidence"].parent,
    )


def progress(paths, session=None):
    plans = recorder.read_csv(paths["plan"])
    results = recorder.read_csv(paths["results"])
    return validator.build_progress(
        plans, results, session_id=session, evidence_base=paths["evidence"].parent,
        results_file_exists=paths["results"].exists(),
        payload_rows=recorder.read_json(paths["payload"]),
        profile_rows=recorder.read_csv(paths["profiles"]),
    )


def test_empty_progress_does_not_create_result_file():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        report = progress(paths)
        assert report["overall_plan_total"] == report["current_session_plan_count"] == 60
        assert report["executed_count"] == report["recorded_count"] == 0
        assert report["not_executed_count"] == 60
        assert report["results_file_exists"] is False
        assert not paths["results"].exists()


def test_progress_counts_and_session_filter():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        save(paths, "PLAN-V2-001", observed(paths))
        save(paths, "PLAN-V2-002", observed(
            paths, result="failure", error_category="timeout", http_status="504"
        ))
        save(paths, "PLAN-V2-003", observed(
            paths, result="blocked_capability", latency_ms="", input_tokens="",
            output_tokens="", total_tokens="", cost_cny="", http_status="",
            finish_reason="", request_id="",
        ))
        report = progress(paths, "SESSION-D1-MORNING")
        assert report["overall_plan_total"] == 60
        assert report["current_session_plan_count"] == 20
        assert report["recorded_count"] == 3
        assert report["executed_count"] == 2
        assert report["not_executed_count"] == 18
        assert report["success_count"] == report["failure_count"] == 1
        assert report["blocked_capability_count"] == 1
        assert report["validated_count"] == 3
        assert report["channels"]["19"]["recorded"] == 3


def test_aborted_stop_rule_and_missing_evidence_counts():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        save(paths, "PLAN-V2-001", observed(
            paths, result="aborted_stop_rule", error_category="cost_anomaly",
            evidence_log_path="missing.png",
        ))
        report = progress(paths)
        assert report["aborted_stop_rule_count"] == 1
        assert report["missing_evidence_count"] == 1
        assert report["stop_condition_triggered"] is True
        assert any(item["rule"] == "abnormal_cost" for item in report["stop_condition_events"])


def test_three_consecutive_429_or_5xx_stops_channel():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        for plan_id, status in zip(
            ("PLAN-V2-001", "PLAN-V2-002", "PLAN-V2-003"), ("429", "500", "503")
        ):
            save(paths, plan_id, observed(
                paths, result="failure", error_category="http_error", http_status=status
            ))
        report = progress(paths)
        assert any(
            item["rule"] == "three_consecutive_429_or_5xx"
            and item["scope_id"] == "19"
            for item in report["stop_condition_events"]
        )


def test_auth_channel_conflict_and_protocol_stop_rules():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        save(paths, "PLAN-V2-001", observed(
            paths, result="failure", error_category="authentication_error", http_status="401"
        ))
        save(paths, "PLAN-V2-002", observed(
            paths, result="aborted_stop_rule", error_category="channel_mismatch"
        ))
        save(paths, "PLAN-V2-004", observed(
            paths, result="failure", error_category="protocol_error",
            ttft_ms="100", sse_complete="FALSE", done_received="FALSE"
        ))
        rules = {item["rule"] for item in progress(paths)["stop_condition_events"]}
        assert {
            "authentication_or_permission_error",
            "planned_channel_evidence_conflict",
            "stream_protocol_incomplete",
        } <= rules


def test_duplicate_orphan_and_metadata_mismatch_detection():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        save(paths, "PLAN-V2-001", observed(paths))
        rows = recorder.read_csv(paths["results"])
        duplicate = dict(rows[0])
        orphan = dict(rows[0], plan_id="PLAN-V2-999", measurement_id="MEAS-ORPHAN")
        mismatch = dict(rows[0], plan_id="PLAN-V2-002", measurement_id="MEAS-MISMATCH",
                        channel_id="wrong")
        report = validator.build_progress(
            recorder.read_csv(paths["plan"]), rows + [duplicate, orphan, mismatch],
            evidence_base=paths["evidence"].parent,
            payload_rows=recorder.read_json(paths["payload"]),
            profile_rows=recorder.read_csv(paths["profiles"]),
        )
        assert report["duplicate_plan_id_count"] == 1
        assert report["result_not_in_plan_count"] == 1
        assert report["plan_field_mismatch_count"] == 1


def test_validator_has_no_network_import_and_never_writes():
    tree = ast.parse(
        (ROOT / "src" / "validate_real_measurement_progress_v2.py").read_text(encoding="utf-8")
    )
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"requests", "httpx", "urllib", "aiohttp"})
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        progress(paths)
        assert not paths["results"].exists()
