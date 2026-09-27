import copy
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import real_channel_shadow_adapter as adapter  # noqa: E402
import run_real_channel_shadow as runner  # noqa: E402


@pytest.fixture
def catalog_rows():
    return runner.read_csv(runner.CATALOG_PATH)


def test_all_five_real_channels_convert(catalog_rows):
    assert len(adapter.adapt_shadow_catalog(catalog_rows)) == 5


def test_identity_fields_map_correctly(catalog_rows):
    source = catalog_rows[0]
    result = adapter.adapt_real_channel(source)
    for field in ("candidate_id", "channel_id", "channel_name", "requested_model", "actual_model", "currency", "confidence_level", "data_source"):
        assert result[field] == source[field]


def test_latency_ms_comes_from_mean(catalog_rows):
    source = catalog_rows[0]
    assert adapter.adapt_real_channel(source)["latency_ms"] == source["latency_mean_ms"]


def test_success_rate_comes_from_observed_rate(catalog_rows):
    source = catalog_rows[0]
    result = adapter.adapt_real_channel(source)
    assert result["success_rate"] == result["observed_success_rate"] == source["observed_success_rate"]


def test_sample_size_comes_from_measurement_count(catalog_rows):
    source = catalog_rows[0]
    assert adapter.adapt_real_channel(source)["sample_size"] == source["measurement_count"]


def test_measured_and_non_mock_markers_preserved(catalog_rows):
    results = adapter.adapt_shadow_catalog(catalog_rows)
    assert all(row["source_type"] == "measured" for row in results)
    assert all(row["is_mock"] == "FALSE" for row in results)


def test_actual_model_remains_pending(catalog_rows):
    assert all(row["actual_model"] == "pending_confirmation" for row in adapter.adapt_shadow_catalog(catalog_rows))


def test_routing_false_and_shadow_true_are_preserved(catalog_rows):
    results = adapter.adapt_shadow_catalog(catalog_rows)
    assert all(row["routing_eligible"] == "FALSE" for row in results)
    assert all(row["shadow_eligible"] == "TRUE" for row in results)


def test_shadow_admission_does_not_enforce_routing_eligibility(catalog_rows):
    assert len(adapter.adapt_shadow_catalog(catalog_rows)) == 5


def test_shadow_false_is_not_admitted(catalog_rows):
    changed = copy.deepcopy(catalog_rows)
    changed[0]["shadow_eligible"] = "FALSE"
    assert len(adapter.adapt_shadow_catalog(changed)) == 4


def test_input_objects_are_not_modified(catalog_rows):
    before = copy.deepcopy(catalog_rows)
    adapter.adapt_shadow_catalog(catalog_rows)
    assert catalog_rows == before


def test_missing_core_field_raises(catalog_rows):
    changed = dict(catalog_rows[0])
    del changed["latency_mean_ms"]
    with pytest.raises(adapter.ShadowAdapterError, match="latency_mean_ms"):
        adapter.adapt_real_channel(changed)


def test_non_measured_or_mock_row_is_rejected(catalog_rows):
    changed = dict(catalog_rows[0], source_type="simulation_parameter")
    with pytest.raises(adapter.ShadowAdapterError, match="measured"):
        adapter.adapt_real_channel(changed)
    changed = dict(catalog_rows[0], is_mock="TRUE")
    with pytest.raises(adapter.ShadowAdapterError, match="FALSE"):
        adapter.adapt_real_channel(changed)
