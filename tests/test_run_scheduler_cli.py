import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src" / "run_scheduler.py"
ARGS = ["--request-id", "CLI", "--model", "deepseek-v4-flash", "--stream", "false", "--input-tokens", "1000", "--output-tokens", "500", "--currency", "CNY", "--strategy", "confidence_aware_v2"]


def test_cli_real_shadow_and_external_cwd(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), *ARGS, "--mode", "real_shadow", "--dry-run"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0 and json.loads(result.stdout)["recommended_candidate"] == "REAL-CHANNEL-19"


def test_cli_real_execute_is_nonzero_and_cannot_be_overridden():
    result = subprocess.run([sys.executable, str(SCRIPT), *ARGS, "--mode", "real_execute", "--dry-run"], capture_output=True, text=True, encoding="utf-8")
    payload = json.loads(result.stdout)
    assert result.returncode != 0 and payload["status"] == "blocked" and payload["execution_attempted"] is False


def test_unknown_strategy_and_mode_nonzero():
    bad_strategy = [*ARGS]
    bad_strategy[-1] = "nope"
    assert subprocess.run([sys.executable, str(SCRIPT), *bad_strategy, "--mode", "simulation", "--dry-run"], capture_output=True).returncode != 0
    assert subprocess.run([sys.executable, str(SCRIPT), *ARGS, "--mode", "nope", "--dry-run"], capture_output=True).returncode != 0


def test_dry_run_writes_no_log(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), *ARGS, "--mode", "simulation", "--dry-run", "--output-dir", str(tmp_path)], capture_output=True)
    assert result.returncode == 0 and not list(tmp_path.iterdir())


def test_request_file_and_custom_log(tmp_path):
    request = tmp_path / "request.json"
    log = tmp_path / "custom.jsonl"
    request.write_text(json.dumps({"request_id": "FILE", "requested_model": "deepseek-v4-flash", "stream": False, "input_tokens": 1, "output_tokens": 1, "currency": "CNY", "strategy": "fastest_first", "mode": "simulation"}), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--request-file", str(request), "--log-path", str(log)], capture_output=True)
    assert result.returncode == 0 and log.exists()
