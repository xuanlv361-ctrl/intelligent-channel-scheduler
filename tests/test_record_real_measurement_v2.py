import ast
import csv
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import record_real_measurement_v2 as recorder  # noqa: E402


PROTECTED = [
    ROOT / "output" / "real_measurement_plan_v2.csv",
    ROOT / "output" / "real_measurement_payloads_v2.json",
    ROOT / "data" / "real_channel_measurements_v1.csv",
    ROOT / "data" / "channel_catalog_real_v1.csv",
    ROOT / "data" / "candidate_channels_v1.csv",
    ROOT / "config" / "strategy_catalog_v2.json",
]


def workspace(base: Path) -> dict[str, Path]:
    paths = {
        "plan": base / "plan.csv",
        "payload": base / "payload.json",
        "profiles": base / "profiles.csv",
        "results": base / "results.csv",
        "backups": base / "backups",
        "evidence": base / "evidence",
    }
    shutil.copyfile(recorder.DEFAULT_PLAN_PATH, paths["plan"])
    shutil.copyfile(recorder.DEFAULT_PAYLOAD_PATH, paths["payload"])
    shutil.copyfile(ROOT / "data" / "request_profiles_v2.csv", paths["profiles"])
    paths["evidence"].mkdir()
    return paths


def actual(paths: dict[str, Path], **changes):
    test = paths["evidence"] / "test.png"
    log = paths["evidence"] / "log.png"
    test.write_bytes(b"manual screenshot placeholder for isolated test")
    log.write_bytes(b"manual log screenshot placeholder for isolated test")
    value = {
        "measured_at": "2026-07-23 10:30:00",
        "result": "success",
        "latency_ms": "800",
        "ttft_ms": "",
        "input_tokens": "20",
        "output_tokens": "60",
        "total_tokens": "80",
        "cost_cny": "0.00014",
        "http_status": "",
        "actual_model": "",
        "request_id": "",
        "finish_reason": "stop",
        "sse_complete": "",
        "done_received": "",
        "error_category": "",
        "evidence_test_path": str(test),
        "evidence_log_path": str(log),
        "notes": "",
    }
    value.update(changes)
    return value


def record(paths: dict[str, Path], plan_id="PLAN-V2-001", **kwargs):
    return recorder.record_result(
        plan_path=paths["plan"], payload_path=paths["payload"],
        profiles_path=paths["profiles"], results_path=paths["results"],
        backup_dir=paths["backups"], plan_id=plan_id,
        evidence_base=paths["evidence"].parent, actual=kwargs.pop("actual", actual(paths)),
        **kwargs,
    )


def test_import_help_and_dry_run_do_not_create_result():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        assert not paths["results"].exists()
        output = record(paths, dry_run=True)
        assert output["written"] is False
        assert not paths["results"].exists()


def test_first_confirmed_function_save_creates_utf8_csv_and_copies_metadata():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        output = record(paths)
        assert paths["results"].exists()
        assert not paths["results"].read_bytes().startswith(b"\xef\xbb\xbf")
        rows = recorder.read_csv(paths["results"])
        assert len(rows) == 1
        row = rows[0]
        plan = recorder.find_unique(recorder.read_csv(paths["plan"]), "plan_id", "PLAN-V2-001")
        assert all(row[field] == plan[field] for field in recorder.PLAN_COPY_FIELDS)
        assert row["planned_stream"] == "FALSE"
        assert row["planned_prompt_version"] == "p01-v1"
        assert row["planned_max_tokens"] == "128"
        assert row["source_type"] == "measured_manual_uat"
        assert row["is_mock"] == "FALSE"
        assert row["validation_status"] == "validated"
        assert row["measurement_id"] == recorder.deterministic_measurement_id("PLAN-V2-001")
        assert output["measurement_id"] == row["measurement_id"]


def test_unknown_and_duplicate_plan_ids_are_rejected():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        with pytest.raises(recorder.MeasurementRecordError, match="unknown plan_id"):
            record(paths, plan_id="PLAN-V2-999")
        record(paths)
        with pytest.raises(recorder.MeasurementRecordError, match="already recorded"):
            record(paths)


def test_amend_creates_timestamped_backup():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        record(paths)
        before = paths["results"].read_bytes()
        output = record(paths, amend=True, actual=actual(paths, latency_ms="900"))
        backup = Path(output["backup_path"])
        assert backup.parent == paths["backups"]
        assert backup.exists() and backup.read_bytes() == before
        assert recorder.read_csv(paths["results"])[0]["latency_ms"] == "900.0"


def test_observation_fields_are_never_inferred():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        record(paths, actual=actual(paths, http_status="", actual_model="", request_id=""))
        row = recorder.read_csv(paths["results"])[0]
        assert row["http_status"] == row["actual_model"] == row["request_id"] == ""


def test_nonstream_rejects_ttft_and_sse_fields():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        with pytest.raises(recorder.MeasurementRecordError, match="non-stream"):
            record(paths, actual=actual(paths, ttft_ms="10"))


def test_p04_uses_real_stream_metadata_and_requires_protocol_observations():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        stream_actual = actual(
            paths, ttft_ms="120", sse_complete="TRUE", done_received="TRUE"
        )
        record(paths, plan_id="PLAN-V2-004", actual=stream_actual)
        row = recorder.read_csv(paths["results"])[0]
        assert row["planned_stream"] == "TRUE"
        assert row["ttft_ms"] == "120.0"
        assert row["sse_complete"] == row["done_received"] == "TRUE"


def test_token_mismatch_preserves_observed_values_and_flags_usage():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        record(paths, actual=actual(paths, input_tokens="20", output_tokens="60", total_tokens="99"))
        row = recorder.read_csv(paths["results"])[0]
        assert row["total_tokens"] == "99"
        assert "usage_mismatch" in row["validation_status"]


def test_failure_requires_error_context_and_blocked_is_not_attempted():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        with pytest.raises(recorder.MeasurementRecordError, match="failure requires"):
            record(paths, actual=actual(paths, result="failure", error_category="", notes=""))
        blocked = actual(
            paths, result="blocked_capability", latency_ms="", input_tokens="",
            output_tokens="", total_tokens="", cost_cny="", finish_reason="",
        )
        record(paths, actual=blocked)
        assert recorder.read_csv(paths["results"])[0]["execution_attempted"] == "FALSE"


def test_missing_evidence_is_not_validated():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        missing = actual(
            paths, evidence_test_path="missing-test.png",
            evidence_log_path="missing-log.png",
        )
        record(paths, actual=missing)
        row = recorder.read_csv(paths["results"])[0]
        assert "missing_evidence" in row["validation_status"]
        assert row["missing_evidence_fields"] == "evidence_test_path|evidence_log_path"


def test_model_and_stream_protocol_mismatches_are_factual_flags():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        observed = actual(
            paths, actual_model="observed-other-model", result="failure",
            error_category="protocol_error", ttft_ms="100",
            sse_complete="FALSE", done_received="FALSE",
        )
        record(paths, plan_id="PLAN-V2-004", actual=observed)
        status = recorder.read_csv(paths["results"])[0]["validation_status"]
        assert "model_mismatch" in status and "protocol_incomplete" in status


def test_atomic_replace_is_used(monkeypatch):
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        called = []
        original = recorder.os.replace

        def tracking_replace(source, target):
            called.append((Path(source), Path(target)))
            return original(source, target)

        monkeypatch.setattr(recorder.os, "replace", tracking_replace)
        record(paths)
        assert called and called[0][1] == paths["results"]
        assert not list(paths["results"].parent.glob("*.tmp"))


def test_chinese_channel_name_round_trips():
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        record(paths, plan_id="PLAN-V2-005")
        assert recorder.read_csv(paths["results"])[0]["channel_name"] == "阿里云ADB-DeepSeek-Test"


def test_no_network_library_imports_and_protected_hashes_stay_fixed():
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    tree = ast.parse((ROOT / "src" / "record_real_measurement_v2.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & {"requests", "httpx", "urllib", "aiohttp"})
    with tempfile.TemporaryDirectory() as directory:
        paths = workspace(Path(directory))
        record(paths)
    after = {path: hashlib.sha256(path.read_bytes()).digest() for path in PROTECTED}
    assert before == after
