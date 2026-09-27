import json
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_management_scripts_are_present_and_use_fixed_same_origin_entrypoint():
    required={
        "install-autostart.ps1","uninstall-autostart.ps1","start-local.ps1",
        "stop-local.ps1","restart-local.ps1","status-local.ps1","logs-local.ps1",
    }
    assert required<={path.name for path in (ROOT/"scripts").glob("*.ps1")}
    common=(ROOT/"scripts"/"local-runtime-common.ps1").read_text(encoding="utf-8")
    start=(ROOT/"scripts"/"start-local.ps1").read_text(encoding="utf-8")
    assert "ICS_LOCAL_CONSOLE_PORT" in common
    assert "else { 5174 }" in common
    assert 'Wait-ConsoleReady' in start
    assert 'npm","run","dev' not in start
    assert 'backend.app:app' in start
    assert 'backend.local_runtime_worker' in start


def test_local_worker_writes_safe_fresh_heartbeat(tmp_path):
    process=subprocess.Popen([
        sys.executable,"-m","backend.local_runtime_worker","--state-dir",str(tmp_path),
        "--interval-seconds","1"],cwd=ROOT)
    heartbeat=tmp_path/"run"/"worker-heartbeat.json"
    try:
        for _ in range(30):
            if heartbeat.exists(): break
            time.sleep(.1)
        payload=json.loads(heartbeat.read_text(encoding="utf-8"))
        assert payload["state"]=="ready"
        assert payload["network_called"] is False
        assert payload["credentials_exposed"] is False
        assert "authorization" not in json.dumps(payload).lower()
    finally:
        process.terminate()
        process.wait(timeout=10)
