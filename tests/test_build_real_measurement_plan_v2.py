import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import build_real_measurement_plan_v2 as plan  # noqa: E402


def inputs():
    return plan.load_json(plan.CONFIG_PATH), plan.read_csv(plan.PROFILES_PATH)


def test_plan_distribution_and_uniqueness():
    config, profiles = inputs()
    rows = plan.build_plan(config, profiles)
    plan.validate_plan(rows, profiles)
    assert len(rows) == 60
    assert Counter(row["session_id"] for row in rows) == Counter({session["session_id"]: 20 for session in config["sessions"]})
    assert set(Counter(row["channel_id"] for row in rows).values()) == {12}
    assert Counter(row["request_profile_id"] for row in rows) == Counter({f"P{i:02d}": 15 for i in range(1, 5)})
    assert len({row["plan_id"] for row in rows}) == 60
    assert len({(row["session_id"], row["channel_id"], row["request_profile_id"]) for row in rows}) == 60


def test_disabled_profiles_actual_fields_and_status():
    config, profiles = inputs()
    rows = plan.build_plan(config, profiles)
    assert not {"P05", "P06"} & {row["request_profile_id"] for row in rows}
    assert all(row["execution_status"] == "not_executed" for row in rows)
    assert all(all(row[field] == "" for field in plan.ACTUAL_FIELDS) for row in rows)
    assert "pending_confirmation" not in json.dumps(rows)


def test_rotation_order():
    config, profiles = inputs()
    rows = plan.build_plan(config, profiles)
    for session in config["sessions"]:
        subset = [row for row in rows if row["session_id"] == session["session_id"]]
        order = []
        for row in subset:
            if row["channel_id"] not in order:
                order.append(row["channel_id"])
        assert order == session["channel_order"]


def test_summary_and_output_schema(tmp_path):
    config, profiles = inputs()
    rows = plan.build_plan(config, profiles)
    summary = plan.build_summary(config, profiles, rows)
    plan.write_outputs(tmp_path, rows, summary)
    assert json.loads((tmp_path / plan.JSON_NAME).read_text(encoding="utf-8"))["real_api_calls_performed"] == 0
    with (tmp_path / plan.CSV_NAME).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == plan.PLAN_COLUMNS and len(list(reader)) == 60


def test_dry_run_and_repeatability(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src" / "build_real_measurement_plan_v2.py"), "--dry-run", "--output-dir", str(tmp_path)], capture_output=True)
    assert result.returncode == 0 and not list(tmp_path.iterdir())
    subprocess.run([sys.executable, str(ROOT / "src" / "build_real_measurement_plan_v2.py"), "--output-dir", str(tmp_path)], check=True, capture_output=True)
    first = [(tmp_path / name).read_bytes() for name in (plan.CSV_NAME, plan.JSON_NAME)]
    subprocess.run([sys.executable, str(ROOT / "src" / "build_real_measurement_plan_v2.py"), "--output-dir", str(tmp_path)], check=True, capture_output=True)
    assert first == [(tmp_path / name).read_bytes() for name in (plan.CSV_NAME, plan.JSON_NAME)]


def test_v1_hash_unchanged_and_no_secrets(tmp_path):
    before = hashlib.sha256(plan.V1_MEASUREMENTS_PATH.read_bytes()).digest()
    subprocess.run([sys.executable, str(ROOT / "src" / "build_real_measurement_plan_v2.py"), "--output-dir", str(tmp_path)], check=True, capture_output=True)
    assert before == hashlib.sha256(plan.V1_MEASUREMENTS_PATH.read_bytes()).digest()
    combined = "".join((tmp_path / name).read_text(encoding="utf-8").lower() for name in (plan.CSV_NAME, plan.JSON_NAME))
    assert not any(term in combined for term in ("api_key", "authorization", "secret_key"))
