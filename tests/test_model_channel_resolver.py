import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from model_channel_resolver import (  # noqa: E402
    ModelChannelResolutionError,
    load_mapping,
    resolve_model_channels,
)
from model_selection_pipeline import build_demo, select_for_request  # noqa: E402


def test_every_catalog_model_has_offline_candidates():
    mapping = load_mapping()
    assert len(mapping["models"]) == 5
    for model_id in mapping["models"]:
        rows = resolve_model_channels(model_id, mapping)
        assert rows
        assert all(row["is_mock"] is True for row in rows)
        assert rows == sorted(rows, key=lambda row: (row["priority"], row["channel_id"]))


def test_unknown_model_rejected():
    with pytest.raises(ModelChannelResolutionError, match="unknown"):
        resolve_model_channels("missing-model")


def test_end_to_end_pipeline_is_deterministic_and_offline():
    first = select_for_request("REQ-1", "Write Python code")
    assert first == select_for_request("REQ-1", "Write Python code")
    assert first["selected_model"]
    assert first["channel_candidates"]
    assert first["final_channel_decision"] == first["channel_candidates"][0]["channel_id"]
    assert first["real_api_called"] is False
    assert first["source_type"] == "offline_demonstration_only"


def test_demo_log_rows_have_complete_dashboard_fields():
    rows = build_demo()
    assert len(rows) == 3
    required = {
        "user_request", "detected_task", "model_ranking", "selected_model",
        "channel_candidates", "final_channel_decision",
    }
    assert all(required <= row.keys() for row in rows)
