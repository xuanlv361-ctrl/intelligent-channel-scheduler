import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_unified_uat_batch_v3.ps1"
SESSION = "SESSION-V3-D1-MORNING"
POWERSHELL = "powershell.exe"


def run_script(tmp_path: Path, *args: str, mocks=None, env_overrides=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("WEIMETA_CHINA_UAT_API_KEY", None)
    env["ROUTING_CONSOLE_TEST_MODE"] = "1"
    env["WEIMETA_UAT_TEST_EVIDENCE_ROOT"] = str(tmp_path / "evidence")
    if mocks is not None:
        mock_path = tmp_path / "mock.json"
        mock_path.write_text(json.dumps(mocks), encoding="utf-8")
        env["WEIMETA_UAT_TEST_TRANSPORT_FILE"] = str(mock_path)
    else:
        env.pop("WEIMETA_UAT_TEST_TRANSPORT_FILE", None)
    env.update(env_overrides or {})
    command = [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
        "-SessionId", SESSION, "-OutputDirectory", str(tmp_path / "output"), *args,
    ]
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def json_documents(stdout: str):
    decoder = json.JSONDecoder()
    documents, index = [], 0
    while index < len(stdout):
        while index < len(stdout) and stdout[index].isspace():
            index += 1
        if index >= len(stdout):
            break
        value, index = decoder.raw_decode(stdout, index)
        documents.append(value)
    return documents


def nonstream(plan_id="UR-V3-001", status=200):
    body = {
        "id": f"resp-{plan_id}", "model": "observed-model",
        "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    return {
        "plan_id": plan_id, "status": status, "content_type": "application/json",
        "request_id": None, "body": json.dumps(body), "latency_ms": 12, "ttft_ms": None,
    }


def stream(plan_id="UR-V3-004"):
    body = "\n".join([
        'data: {"id":"resp-stream","model":"observed-model","choices":[{"delta":{"content":"ok"},"finish_reason":null}]}',
        'data: {"id":"resp-stream","model":"observed-model","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}',
        "data: [DONE]", "",
    ])
    return {
        "plan_id": plan_id, "status": 200, "content_type": "text/event-stream",
        "request_id": "request-observed", "body": body, "latency_ms": 25, "ttft_ms": 5,
    }


def test_dry_run_is_default_and_writes_nothing(tmp_path):
    result = run_script(tmp_path, "-MaxRequests", "4")
    assert result.returncode == 0, result.stderr
    preview, summary = json_documents(result.stdout)
    assert preview["request_count"] == 4
    assert preview["profile_distribution"] == {"P01": 1, "P02": 1, "P03": 1, "P04": 1}
    assert preview["stream_distribution"] == {"false": 3, "true": 1}
    assert summary["network_called"] is False and summary["attempted"] == 0
    assert not (tmp_path / "output").exists()
    assert "Authorization" not in result.stdout


def test_mock_execute_records_nonstream_and_p04_sse_without_network(tmp_path):
    result = run_script(
        tmp_path, "-ConfirmExecute", "-MaxRequests", "4", "-DelaySeconds", "1",
        mocks=[nonstream("UR-V3-001"), nonstream("UR-V3-002"), nonstream("UR-V3-003"), stream()],
    )
    assert result.returncode == 0, result.stderr
    summary = json_documents(result.stdout)[-1]
    assert summary["attempted"] == 4 and summary["succeeded"] == 4
    records = [json.loads(line) for line in (tmp_path / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 4 and all(r["source_type"] == "measured_unified_uat" for r in records)
    p04 = next(r for r in records if r["request_profile_id"] == "P04")
    assert p04["stream"] is True and p04["sse_complete"] is True and p04["done_received"] is True
    assert p04["ttft_ms"] == 5 and p04["actual_channel"] is None
    assert all(r["network_called"] is False and r["is_mock"] is True for r in records)
    metadata = json.loads(next((tmp_path / "evidence").glob("*/request_metadata.json")).read_text(encoding="utf-8"))
    assert all("content" not in message for message in metadata["messages"])
    assert all({"content_length", "content_sha256"} <= set(message) for message in metadata["messages"])
    assert metadata["authorization_saved"] is False


def test_401_stops_batch_and_failed_record_is_retained(tmp_path):
    first = nonstream("UR-V3-001", 401)
    result = run_script(tmp_path, "-ConfirmExecute", "-MaxRequests", "2", "-DelaySeconds", "1", mocks=[first])
    assert result.returncode == 0, result.stderr
    summary = json_documents(result.stdout)[-1]
    assert summary["attempted"] == 1 and summary["failed"] == 1
    assert summary["stop_reason"] == "uat_authentication_failed"
    record = json.loads((tmp_path / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8"))
    assert record["http_status"] == 401 and record["error_category"] == "uat_authentication_failed"


def test_429_and_three_consecutive_failures_stop_without_retry(tmp_path):
    limited = run_script(
        tmp_path / "limited", "-ConfirmExecute", "-MaxRequests", "2", "-DelaySeconds", "1",
        mocks=[nonstream("UR-V3-001", 429)],
    )
    assert json_documents(limited.stdout)[-1]["stop_reason"] == "rate_limited"
    failures = [nonstream(f"UR-V3-00{i}", 500) for i in range(1, 4)]
    stopped = run_script(
        tmp_path / "failures", "-ConfirmExecute", "-MaxRequests", "4", "-DelaySeconds", "1",
        mocks=failures,
    )
    summary = json_documents(stopped.stdout)[-1]
    assert summary["attempted"] == 3 and summary["failed"] == 3
    assert summary["stop_reason"] == "three_consecutive_failures"


def test_token_anomaly_stops_and_is_recorded(tmp_path):
    bad = nonstream()
    payload = json.loads(bad["body"])
    payload["usage"]["total_tokens"] = 999
    bad["body"] = json.dumps(payload)
    result = run_script(
        tmp_path, "-ConfirmExecute", "-MaxRequests", "2", "-DelaySeconds", "1", mocks=[bad],
    )
    summary = json_documents(result.stdout)[-1]
    assert summary["attempted"] == 1 and summary["stop_reason"] == "token_or_cost_anomaly"
    record = json.loads((tmp_path / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8"))
    assert record["error_category"] == "token_or_cost_anomaly"


def test_resume_skips_completed_and_amend_creates_backup(tmp_path):
    first = run_script(tmp_path, "-ConfirmExecute", "-MaxRequests", "1", "-DelaySeconds", "1", mocks=[nonstream()])
    assert first.returncode == 0, first.stderr
    resumed = run_script(tmp_path, "-ConfirmExecute", "-MaxRequests", "1", "-DelaySeconds", "1", mocks=[nonstream("UR-V3-002")])
    assert resumed.returncode == 0, resumed.stderr
    assert json_documents(resumed.stdout)[-1]["skipped"] == 1
    amended = run_script(tmp_path, "-ConfirmExecute", "-Amend", "-MaxRequests", "1", "-DelaySeconds", "1", mocks=[nonstream()])
    assert amended.returncode == 0, amended.stderr
    assert list((tmp_path / "output" / "backups").glob("*before_amend*.jsonl"))
    records = [json.loads(line) for line in (tmp_path / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2 and len({r["plan_id"] for r in records}) == 2


def test_execute_without_key_or_authorized_mock_is_blocked(tmp_path):
    result = run_script(tmp_path, "-ConfirmExecute", "-MaxRequests", "1")
    assert result.returncode == 0
    record = json.loads((tmp_path / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8"))
    assert record["network_called"] is False
    assert record["transport_stage"] == "key_validation"
    assert record["transport_error_code"] == "invalid_api_key"


def run_self_test(tmp_path: Path, **overrides):
    env = os.environ.copy()
    env.pop("WEIMETA_CHINA_UAT_API_KEY", None)
    env["ROUTING_CONSOLE_TEST_MODE"] = "1"
    env.update(overrides)
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT), "-SelfTestTransport"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def test_windows_powershell_transport_construction_self_test(tmp_path):
    result = run_self_test(tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "status": "passed", "assembly": "System.Net.Http",
        "request_construction": "passed", "redaction": "passed",
        "network_called": False, "result_written": False,
    }


def test_missing_system_net_http_has_safe_assembly_diagnostic(tmp_path):
    result = run_self_test(tmp_path, WEIMETA_UAT_TEST_FORCE_ASSEMBLY_FAILURE="1")
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["transport_stage"] == "assembly_load"
    assert payload["transport_error_code"] == "system_net_http_load_failed"
    assert payload["network_called"] is False


@pytest.mark.parametrize("key", ["bad key", "abc\r\nInjected: value", "密钥"])
def test_invalid_keys_are_rejected_before_network_and_redacted(tmp_path, key):
    result = run_script(
        tmp_path, "-ConfirmExecute", "-MaxRequests", "1",
        env_overrides={"WEIMETA_CHINA_UAT_API_KEY": key},
    )
    assert result.returncode == 0, result.stderr
    record = json.loads((tmp_path / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8"))
    assert record["network_called"] is False
    assert record["transport_stage"] == "key_validation"
    combined = result.stdout + result.stderr + json.dumps(record, ensure_ascii=False)
    assert key not in combined


def test_construction_and_send_failures_report_correct_network_semantics(tmp_path):
    secret = "TEST_PRINTABLE_SECRET_123"
    construction = run_script(
        tmp_path / "construction", "-ConfirmExecute", "-MaxRequests", "1",
        env_overrides={
            "WEIMETA_CHINA_UAT_API_KEY": secret,
            "WEIMETA_UAT_TEST_FORCE_CONSTRUCTION_FAILURE": "1",
        },
    )
    construction_record = json.loads(
        (tmp_path / "construction" / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8")
    )
    assert construction_record["transport_stage"] == "request_construction"
    assert construction_record["network_called"] is False
    send = run_script(
        tmp_path / "send", "-ConfirmExecute", "-MaxRequests", "1",
        env_overrides={
            "WEIMETA_CHINA_UAT_API_KEY": secret,
            "WEIMETA_UAT_TEST_FORCE_SEND_FAILURE": "1",
        },
    )
    send_record = json.loads(
        (tmp_path / "send" / "output" / "unified_uat_execution_v3.jsonl").read_text(encoding="utf-8")
    )
    assert send_record["transport_stage"] == "send_request"
    assert send_record["network_called"] is True
    all_text = construction.stdout + construction.stderr + send.stdout + send.stderr
    all_text += json.dumps(construction_record) + json.dumps(send_record)
    for path in list((tmp_path / "construction").rglob("*")) + list((tmp_path / "send").rglob("*")):
        if path.is_file():
            all_text += path.read_text(encoding="utf-8", errors="replace")
    assert secret not in all_text


@pytest.mark.parametrize("value", ["0", "21"])
def test_hard_request_limit(value, tmp_path):
    result = run_script(tmp_path, "-MaxRequests", value)
    assert result.returncode != 0
