import csv
import hashlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import build_scenario_candidates as builder  # noqa: E402


def generated():
    return builder.build(dry_run=True)


def scenario(rows, sid):
    return {row["candidate_id"]: row for row in rows if row["scenario_id"] == sid}


def selected(rows, sid):
    return [row["candidate_id"] for row in rows if row["scenario_id"] == sid and row["expected_selected"] == "TRUE"]


def test_counts_and_scenarios():
    rows = generated()
    assert len(rows) == 43
    assert len({row["scenario_id"] for row in rows}) == 15
    assert sum(row["expected_selected"] == "TRUE" for row in rows) == 13
    assert sum(row["expected_eligible"] == "FALSE" for row in rows) == 15


def test_joint_key_unique():
    keys = [(r["scenario_id"], r["candidate_id"]) for r in generated()]
    assert len(keys) == len(set(keys))


def test_output_columns_in_fixed_order(tmp_path):
    target = tmp_path / "out.csv"
    builder.build(target)
    with target.open(encoding="utf-8", newline="") as handle:
        assert next(csv.reader(handle)) == builder.OUTPUT_COLUMNS


def test_s001_latency_selection():
    assert selected(generated(), "S001") == ["MOCK-DS-FAST"]


def test_s002_cost_selection():
    assert selected(generated(), "S002") == ["MOCK-DS-CHEAP"]


def test_s003_stream_filter():
    rows = scenario(generated(), "S003")
    assert selected(generated(), "S003") == ["MOCK-DS-FAST"]
    assert rows["REAL-DS-48"]["expected_exclusion_reason"] == "stream_capability_unknown"
    assert rows["MOCK-DS-CHEAP"]["expected_exclusion_reason"] == "stream_not_supported"


def test_s004_unroutable():
    rows = scenario(generated(), "S004")
    assert not selected(generated(), "S004")
    assert {r["expected_exclusion_reason"] for r in rows.values()} == {"stream_not_supported"}


def test_s005_unavailable_filter():
    rows = scenario(generated(), "S005")
    assert rows["MOCK-DS-FAST"]["expected_exclusion_reason"] == "availability_unavailable"
    assert selected(generated(), "S005") == ["MOCK-DS-CHEAP"]


def test_s006_missing_latency_filter():
    rows = scenario(generated(), "S006")
    assert rows["MOCK-DS-FAST"]["latency_ms"] == ""
    assert rows["MOCK-DS-FAST"]["expected_exclusion_reason"] == "missing_latency"
    assert selected(generated(), "S006") == ["MOCK-DS-CHEAP"]


def test_s007_missing_price_filter():
    rows = scenario(generated(), "S007")
    cheap = rows["MOCK-DS-CHEAP"]
    assert cheap["input_price_per_1m"] == cheap["output_price_per_1m"] == ""
    assert cheap["expected_exclusion_reason"] == "missing_price"
    assert selected(generated(), "S007") == ["MOCK-DS-FAST"]


def test_s008_stale_filter():
    rows = scenario(generated(), "S008")
    assert rows["MOCK-DS-FAST"]["expected_exclusion_reason"] == "stale_metrics"
    assert selected(generated(), "S008") == ["MOCK-DS-CHEAP"]


def test_s009_small_sample_penalty_only():
    real = scenario(generated(), "S009")["REAL-DS-48"]
    assert (real["latency_ms"], real["sample_size"], real["confidence_level"]) == ("300", "1", "medium")
    assert real["expected_eligible"] == "TRUE"
    assert selected(generated(), "S009") == ["MOCK-DS-FAST"]


def test_s010_low_confidence_penalty_only():
    real = scenario(generated(), "S010")["REAL-DS-48"]
    assert (real["latency_ms"], real["sample_size"], real["confidence_level"]) == ("300", "100", "low")
    assert real["expected_eligible"] == "TRUE"
    assert selected(generated(), "S010") == ["MOCK-DS-FAST"]


def test_s011_priority_tie_breaker():
    rows = scenario(generated(), "S011")
    assert rows["TIE-A"]["priority"] == "10" and rows["TIE-B"]["priority"] == "20"
    assert selected(generated(), "S011") == ["TIE-A"]


def test_s012_channel_id_tie_breaker():
    rows = scenario(generated(), "S012")
    assert rows["TIE-001"]["priority"] == rows["TIE-002"]["priority"] == "10"
    assert selected(generated(), "S012") == ["TIE-001"]


def test_s013_currency_filter():
    rows = scenario(generated(), "S013")
    assert rows["MOCK-DS-CHEAP"]["expected_exclusion_reason"] == "currency_mismatch"
    assert selected(generated(), "S013") == ["MOCK-DS-FAST"]


def test_s014_all_unavailable():
    rows = scenario(generated(), "S014")
    assert not selected(generated(), "S014")
    assert {r["expected_exclusion_reason"] for r in rows.values()} == {"availability_unavailable"}


def test_s015_model_filter():
    rows = scenario(generated(), "S015")
    assert selected(generated(), "S015") == ["REAL-DS-48"]
    assert all(rows[c]["expected_exclusion_reason"] == "model_mismatch" for c in ("MOCK-DS-FAST", "MOCK-DS-CHEAP"))


def test_dry_run_does_not_write(tmp_path):
    target = tmp_path / "never.csv"
    builder.build(target, dry_run=True)
    assert not target.exists()


def test_custom_output_does_not_touch_default(tmp_path):
    before = builder.DEFAULT_OUTPUT.read_bytes()
    target = tmp_path / "scenario.csv"
    builder.build(target)
    assert target.exists() and builder.DEFAULT_OUTPUT.read_bytes() == before


def test_input_csvs_are_not_modified(tmp_path):
    inputs = [builder.DEFAULT_CANDIDATES, builder.DEFAULT_SCENARIOS]
    before = {p: hashlib.sha256(p.read_bytes()).digest() for p in inputs}
    builder.build(tmp_path / "out.csv")
    assert before == {p: hashlib.sha256(p.read_bytes()).digest() for p in inputs}


def test_all_selected_candidates_are_eligible():
    assert all(r["expected_eligible"] == "TRUE" for r in generated() if r["expected_selected"] == "TRUE")


def test_scenario_overrides_are_explicit_mock_design_data():
    rows = generated()
    changed = [("S004", "REAL-DS-48"), ("S009", "REAL-DS-48"), ("S010", "REAL-DS-48")]
    for sid, cid in changed:
        row = scenario(rows, sid)[cid]
        assert "mock_scenario_design" in row["data_source"] and row["is_mock"] == "TRUE"


def test_cli_resolves_project_paths_outside_working_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "build_scenario_candidates.py"), "--dry-run"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0 and "43 rows" in result.stdout
