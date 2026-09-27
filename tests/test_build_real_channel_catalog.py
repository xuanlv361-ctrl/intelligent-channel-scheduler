import copy
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import build_real_channel_catalog as builder  # noqa: E402


@pytest.fixture
def rows():
    return builder.read_measurements(builder.DEFAULT_INPUT)


def test_reads_25_measurements(rows):
    assert len(rows) == 25


def test_identifies_five_channels_and_five_rows_each(rows):
    counts = Counter(row["channel_id"] for row in rows)
    assert counts == Counter({channel: 5 for channel in builder.EXPECTED_CHANNELS})


def test_complete_input_validates(rows):
    builder.validate_measurements(rows)


def test_missing_measurement_id_stops(rows):
    with pytest.raises(builder.ValidationError, match="RM001-RM025"):
        builder.validate_measurements(rows[:-1])


def test_empty_measurement_id_stops(rows):
    changed = copy.deepcopy(rows)
    changed[0]["measurement_id"] = ""
    with pytest.raises(builder.ValidationError, match="non-empty"):
        builder.validate_measurements(changed)


def test_duplicate_measurement_id_stops(rows):
    changed = copy.deepcopy(rows)
    changed[1]["measurement_id"] = changed[0]["measurement_id"]
    with pytest.raises(builder.ValidationError, match="must be unique"):
        builder.validate_measurements(changed)


def test_duplicate_round_channel_stops(rows):
    changed = copy.deepcopy(rows)
    changed[5]["test_round"], changed[5]["channel_id"] = changed[0]["test_round"], changed[0]["channel_id"]
    with pytest.raises(builder.ValidationError, match=r"test_round\+channel_id"):
        builder.validate_measurements(changed)


def test_channel_below_five_stops(rows):
    with pytest.raises(builder.ValidationError, match="must have 5 measurements"):
        builder.validate_measurements([row for row in rows if row["measurement_id"] != "RM025"])


def test_round_below_five_stops(rows):
    with pytest.raises(builder.ValidationError, match="round 5 must have 5"):
        builder.validate_measurements([row for row in rows if row["measurement_id"] != "RM025"])


@pytest.mark.parametrize(
    "channel,expected",
    [
        ("19", (732.0, 710.0, 570.0, 850.0)),
        ("27", (2264.0, 2120.0, 1910.0, 3030.0)),
        ("45", (2568.0, 1340.0, 1090.0, 7370.0)),
        ("48", (826.0, 870.0, 680.0, 970.0)),
        ("50", (1712.0, 1590.0, 1540.0, 2140.0)),
    ],
)
def test_channel_statistics(rows, channel, expected):
    record = next(row for row in builder.build_catalog(rows) if row["channel_id"] == channel)
    actual = tuple(float(record[field]) for field in ("latency_mean_ms", "latency_p50_ms", "latency_min_ms", "latency_max_ms"))
    assert actual == expected


def test_nearest_rank_p50():
    assert builder.percentile_nearest_rank([1090, 1170, 1870, 1340, 7370], 0.5) == 1340


def test_rm022_is_retained_in_channel_45(rows):
    grouped = builder.group_measurements(rows)
    assert any(row["measurement_id"] == "RM022" for row in grouped["45"])
    assert max(float(row["latency_ms"]) for row in grouped["45"]) == 7370


def test_rm022_alteration_is_rejected(rows):
    changed = copy.deepcopy(rows)
    next(row for row in changed if row["measurement_id"] == "RM022")["latency_ms"] = "1370"
    with pytest.raises(builder.ValidationError, match="RM022"):
        builder.validate_measurements(changed)


def test_pending_and_missing_evidence_is_preserved(rows):
    catalog = builder.build_catalog(rows)
    assert all(row["actual_model"] == "pending_confirmation" for row in catalog)
    assert all(row["supports_stream"] == "pending_confirmation" for row in catalog)
    assert all(not row["request_id"] for row in rows)


def test_observed_rate_has_non_long_term_name(rows):
    catalog = builder.build_catalog(rows)
    assert all(row["observed_success_rate"] == "1.000000" for row in catalog)
    assert "long_term_success_rate" not in builder.CATALOG_COLUMNS


def test_routing_is_shadow_only(rows):
    catalog = builder.build_catalog(rows)
    assert all(row["routing_eligible"] == "FALSE" for row in catalog)
    assert all(row["shadow_eligible"] == "TRUE" for row in catalog)
    assert all(row["routing_block_reason"] == builder.ROUTING_BLOCK_REASON for row in catalog)


def test_catalog_evidence_markers(rows):
    catalog = builder.build_catalog(rows)
    assert all(row["source_type"] == "measured" for row in catalog)
    assert all(row["is_mock"] == "FALSE" for row in catalog)
    assert all(row["confidence_level"] == "low" for row in catalog)


def test_token_prices_are_evidence_consistent(rows):
    catalog = builder.build_catalog(rows)
    assert {(row["input_price_per_1m"], row["output_price_per_1m"]) for row in catalog} == {("1.000000000", "2.000000000")}
    assert all(row["cache_read_price_per_1m"] == "pending_confirmation" for row in catalog)


def test_catalog_sort_and_column_order(tmp_path, rows):
    catalog = builder.build_catalog(rows)
    builder.write_csv(tmp_path / "catalog.csv", catalog)
    with (tmp_path / "catalog.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        written = list(reader)
    assert reader.fieldnames == builder.CATALOG_COLUMNS
    assert [row["channel_id"] for row in written] == ["19", "27", "45", "48", "50"]


def test_summary_structure_and_limitations(rows):
    catalog = builder.build_catalog(rows)
    summary = builder.build_summary(catalog, rows, builder.DEFAULT_INPUT)
    required = {"catalog_version", "source_file", "source_file_sha256", "measurement_count", "channel_count", "round_count", "observed_success_count", "observed_failure_count", "actual_model_status", "http_status_status", "request_id_status", "routing_readiness", "channel_summaries", "limitations"}
    assert required <= set(summary)
    assert summary["routing_readiness"] == "shadow_only"
    assert len(summary["limitations"]) >= 8


def test_repeated_output_is_byte_identical(tmp_path, rows):
    catalog = builder.build_catalog(rows)
    summary = builder.build_summary(catalog, rows, builder.DEFAULT_INPUT)
    csv_path, json_path = tmp_path / "catalog.csv", tmp_path / "summary.json"
    builder.write_csv(csv_path, catalog)
    builder.write_json(json_path, summary)
    first = (csv_path.read_bytes(), json_path.read_bytes())
    builder.write_csv(csv_path, catalog)
    builder.write_json(json_path, summary)
    assert first == (csv_path.read_bytes(), json_path.read_bytes())


def test_dry_run_writes_nothing(tmp_path):
    catalog_path, summary_path = tmp_path / "catalog.csv", tmp_path / "summary.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "build_real_channel_catalog.py"), "--input", str(builder.DEFAULT_INPUT), "--catalog-output", str(catalog_path), "--summary-output", str(summary_path), "--dry-run"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert not catalog_path.exists() and not summary_path.exists()


def test_custom_paths_and_external_working_directory(tmp_path):
    catalog_path, summary_path = tmp_path / "nested" / "catalog.csv", tmp_path / "other" / "summary.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "build_real_channel_catalog.py"), "--input", str(builder.DEFAULT_INPUT), "--catalog-output", str(catalog_path), "--summary-output", str(summary_path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0 and catalog_path.exists() and summary_path.exists()


def test_invalid_cli_input_writes_no_outputs(tmp_path, rows):
    bad_input = tmp_path / "bad.csv"
    with bad_input.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows[:-1])
    catalog_path, summary_path = tmp_path / "catalog.csv", tmp_path / "summary.json"
    result = subprocess.run([sys.executable, str(ROOT / "src" / "build_real_channel_catalog.py"), "--input", str(bad_input), "--catalog-output", str(catalog_path), "--summary-output", str(summary_path)], capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert json.loads(result.stderr)["status"] == "error"
    assert not catalog_path.exists() and not summary_path.exists()


def test_input_hash_unchanged_by_build(tmp_path):
    before = hashlib.sha256(builder.DEFAULT_INPUT.read_bytes()).digest()
    result = subprocess.run([sys.executable, str(ROOT / "src" / "build_real_channel_catalog.py"), "--input", str(builder.DEFAULT_INPUT), "--catalog-output", str(tmp_path / "catalog.csv"), "--summary-output", str(tmp_path / "summary.json")], capture_output=True)
    assert result.returncode == 0
    assert before == hashlib.sha256(builder.DEFAULT_INPUT.read_bytes()).digest()


def test_source_has_no_network_clients_or_secrets():
    source = (ROOT / "src" / "build_real_channel_catalog.py").read_text(encoding="utf-8")
    forbidden = ("requests", "httpx", "urllib", "socket", "aiohttp", "Authorization", "API_KEY")
    assert all(token not in source for token in forbidden)


def test_outputs_contain_no_secret_markers(tmp_path, rows):
    catalog = builder.build_catalog(rows)
    summary = builder.build_summary(catalog, rows, builder.DEFAULT_INPUT)
    builder.write_csv(tmp_path / "catalog.csv", catalog)
    builder.write_json(tmp_path / "summary.json", summary)
    combined = (tmp_path / "catalog.csv").read_text(encoding="utf-8") + (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert all(token not in combined for token in ("Authorization", "api_key", "secret_key"))
