import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "data" / "metric_dictionary_v2.csv"


def test_metric_dictionary_v2_covers_required_enterprise_semantics():
    with PATH.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows and {row["dictionary_version"] for row in rows} == {
        "metric_dictionary_v2"
    }
    identifiers = {row["metric_id"] for row in rows}
    assert {
        "model_id", "model_version", "channel_id", "execution_identity",
        "request_type", "modality", "nonstream_total_latency_ms",
        "stream_ttft_ms", "stream_completion_latency_ms", "input_tokens",
        "output_tokens", "currency", "input_unit_price", "output_unit_price",
        "billing_unit", "request_cost", "price_version",
        "price_effective_from", "price_effective_until", "http_status",
        "normalized_error_class", "sample_count", "success_count",
        "success_rate", "source_reliability", "freshness_state",
        "snapshot_version", "snapshot_updated_at",
    } <= identifiers


def test_every_metric_has_timestamp_and_privacy_semantics():
    with PATH.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert all(row["timestamp_semantics"] for row in rows)
    assert all(row["privacy_classification"] for row in rows)
    serialized = PATH.read_text(encoding="utf-8").casefold()
    assert "api key" not in serialized
    assert "authorization" not in serialized
    assert "cookie" not in serialized
