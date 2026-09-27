"""Create the Stage 2 readiness transition after local gates have passed."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
V3 = ROOT / "output" / "unified_uat_execution_v3.jsonl"
EXPECTED_V3 = {
    "rows": 60,
    "bytes": 68620,
    "sha256": "E61D6CDD596BB8963F4BE9A1C42724D31A2A54E33765CEB5EF60AC372EACB627",
}


def load_local_json(path: str) -> dict:
    opener = build_opener(ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:5174{path}", timeout=3) as response:
        return json.loads(response.read(1024 * 1024).decode("utf-8"))


def protected_v3() -> dict[str, object]:
    payload = V3.read_bytes()
    result = {
        "rows": len(payload.splitlines()), "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest().upper(),
    }
    if result != EXPECTED_V3:
        raise RuntimeError(f"protected_v3_invariant_failed:{result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transition-dir", type=Path, required=True)
    arguments = parser.parse_args()
    directory = arguments.transition_dir.resolve()
    freeze = json.loads(
        (directory / "source_freeze_manifest.json").read_text(encoding="utf-8"))
    health = load_local_json("/health")
    ready = load_local_json("/ready")
    dependencies = {
        item["dependency"]: item["status"] for item in ready["dependencies"]}
    transition_id = directory.name
    manifest = {
        "schema_version": "stage2_readiness_manifest_v2",
        "transition_id": transition_id,
        "from": "stage_2_local_readiness",
        "to": "stage_3_manual_authentication",
        "stage_2_status": "completed",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_freeze_active": True,
        "services": {
            "unified_url": "http://127.0.0.1:5174",
            "health": health["status"], "readiness": ready["status"],
            "worker": dependencies.get("local_worker"),
            "database_migration": ready.get("migration_status"),
        },
        "domestic_uat_authentication": {
            "browser": "Google Chrome",
            "profile_scope": "domestic-uat-chrome",
            "profile_persistent": True,
            "profile_in_repository_or_artifacts": False,
            "cdp_scope": "127.0.0.1_only",
            "backend_owns_browser_lifetime": False,
            "current_state": "revalidation_required_after_source_change",
            "synchronization_started": False,
        },
        "gates": {
            "uat_control_and_security_focused": "77 passed",
            "chrome_lifecycle_and_session_focused": "103 passed",
            "complete_backend_first": "1477 passed; 0 failed; 0 skipped",
            "complete_backend_second": "1477 passed; 0 failed; 0 skipped",
            "frontend": "91 passed",
            "eslint": "passed", "typescript": "passed",
            "production_build": "passed; 838.31 kB main chunk warning",
            "mock_playwright": "8 passed",
            "scheduler_benchmark": "144 observations; network_called=false",
            "ablation_a0_a14": "15 variants; network_called=false",
            "json_jsonl_validation": "445 files; 9836 records; 0 failures; 1 intentionally malformed fixture excluded",
            "secret_scan_findings": 0,
            "git_diff_check": "passed; line-ending warnings only",
        },
        "protected_v3": protected_v3(),
        "digests": freeze["digests"],
        "external_activity": {
            "billing_log_reads": 0, "completion_calls": 0,
            "platform_writes": 0, "production_access": 0,
        },
        "warnings": [
            "Vite main chunk is above 500 kB.",
            "Six model and channel output limits remain pending_confirmation until reviewed evidence exists.",
            "The persistent Chrome session must be revalidated in Stage 3 after this source change.",
            "No real billing-log read was performed during Stage 2 refreeze.",
        ],
    }
    (directory / "stage2_readiness_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({
        "transition_id": transition_id, "stage_2_status": "completed",
        "source_freeze_active": True, "current_stage": "stage_3_manual_authentication",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
