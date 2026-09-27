import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from metrics_store import MetricsStore, MetricsValidationError  # noqa: E402


def valid_snapshot():
    return json.loads(
        (ROOT / "data" / "channel_metrics_v1.json").read_text(encoding="utf-8")
    )


def test_load_valid_snapshot_and_preserve_version():
    store = MetricsStore.load()
    assert store.metrics_version == "v1.0.0"
    assert store.updated_at == "2026-07-24T10:00:00+08:00"
    assert len(store.channels()) == 3
    assert store.get("M001")["metrics"]["success_rate"] == 0.97


def test_source_is_read_only_and_lookup_returns_copy():
    path = ROOT / "data" / "channel_metrics_v1.json"
    before = hashlib.sha256(path.read_bytes()).digest()
    store = MetricsStore.load(path)
    row = store.get("M001")
    row["metrics"]["success_rate"] = 0
    assert store.get("M001")["metrics"]["success_rate"] == 0.97
    assert before == hashlib.sha256(path.read_bytes()).digest()


@pytest.mark.parametrize(
    "mutation,match",
    [
        (lambda value: value.pop("metrics_version"), "metrics_version"),
        (lambda value: value["channels"][0]["metrics"].pop("avg_cost"), "avg_cost"),
        (lambda value: value["channels"][0].pop("sample_count"), "sample_count"),
        (lambda value: value["channels"].append(copy.deepcopy(value["channels"][0])), "unique"),
    ],
)
def test_reject_missing_or_invalid_required_fields(mutation, match):
    snapshot = valid_snapshot()
    mutation(snapshot)
    with pytest.raises(MetricsValidationError, match=match):
        MetricsStore(snapshot)


def test_reject_invalid_metric_ranges():
    snapshot = valid_snapshot()
    snapshot["channels"][0]["metrics"]["success_rate"] = 1.1
    with pytest.raises(MetricsValidationError, match="success_rate"):
        MetricsStore(snapshot)
