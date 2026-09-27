from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backend.historical_uat_csv_adapter import (
    CSV_HEADERS,
    HistoricalUatCsvAdapter,
    safe_schema_mapping,
    within_window,
)
from backend.historical_uat_reconciliation import (
    V3Evidence,
    project_metric_events,
    reconcile,
)


def write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADERS)
        writer.writerows(rows)


def row(
    *,
    timestamp: str = "2026-07-29 10:10:22",
    model: str = "deepseek-v4-flash",
    duration: str = "2s (非流)",
    input_tokens: str = "10",
    output_tokens: str = "79",
) -> list[str]:
    return [
        timestamp,
        "mock-credential-placeholder",
        "消费",
        model,
        duration,
        input_tokens,
        output_tokens,
        "¥0.000044",
        "203.0.113.42",
        "sensitive free-form details",
    ]


def v3(
    plan_id: str = "UR-V3-001",
    *,
    completed_at: str = "2026-07-29T02:10:23.0066139+00:00",
    stream: bool = False,
    input_tokens: int = 10,
    output_tokens: int = 79,
    latency_ms: float = 2051.8,
) -> V3Evidence:
    item = {
        "plan_id": plan_id,
        "execution_id": f"EXEC-{plan_id}",
        "started_at": "2026-07-29T02:10:20.8101139+00:00",
        "completed_at": completed_at,
        "requested_model": "deepseek-v4-flash",
        "actual_model": "deepseek-v4-flash",
        "request_profile_id": "P01",
        "stream": stream,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "http_status": 200,
    }
    return V3Evidence(
        item=item,
        started_china="2026-07-29T10:10:20.810113+08:00",
        completed_china="2026-07-29T10:10:23.006614+08:00",
    )


def test_adapter_parses_reviewed_schema_and_discards_sensitive_values(tmp_path):
    source = tmp_path / "export.csv"
    secret = "mock-credential-placeholder"
    ip = "203.0.113.42"
    write_csv(source, [row()])
    accepted, rejected, metadata = HistoricalUatCsvAdapter(source).read()
    assert not rejected
    assert accepted[0].timestamp_utc == "2026-07-29T02:10:22+00:00"
    assert accepted[0].stream is False
    assert accepted[0].input_tokens == 10
    serialized = json.dumps(
        {
            "rows": [item.safe_dict() for item in accepted],
            "metadata": metadata,
            "mapping": safe_schema_mapping(),
        },
        ensure_ascii=False,
    )
    assert secret not in serialized
    assert ip not in serialized
    assert metadata["sensitive_columns_discarded"] == ["API Key", "IP"]


def test_adapter_combines_explicit_cached_tokens_and_normalizes_reviewed_alias(tmp_path):
    source = tmp_path / "export.csv"
    write_csv(
        source,
        [
            row(
                model="deepseek-ai/DeepSeek-V4-Flash",
                duration="3s / 0.1s (流)",
                input_tokens="49 (缓存读: 256)",
            )
        ],
    )
    accepted, rejected, _ = HistoricalUatCsvAdapter(source).read()
    assert not rejected
    parsed = accepted[0]
    assert parsed.normalized_model == "deepseek-v4-flash"
    assert parsed.stream is True
    assert parsed.ttft_ms == 100
    assert parsed.input_uncached_tokens == 49
    assert parsed.input_cached_tokens == 256
    assert parsed.input_tokens == 305


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    [
        (3, "unreviewed-model", "unknown_model_alias"),
        (4, "fast", "invalid_latency_format"),
        (5, "ten", "invalid_input_tokens_format"),
        (6, "many", "invalid_output_tokens_format"),
        (7, "$1", "invalid_cost_format"),
    ],
)
def test_adapter_rejects_unknown_formats_with_reason_codes(
    tmp_path, column, value, reason
):
    source = tmp_path / "export.csv"
    values = row()
    values[column] = value
    write_csv(source, [values])
    accepted, rejected, _ = HistoricalUatCsvAdapter(source).read()
    assert not accepted
    assert rejected[0].reason_code == reason
    assert "credential-placeholder" not in json.dumps(rejected[0].safe_dict())
    assert "203.0.113.42" not in json.dumps(rejected[0].safe_dict())


def test_authorized_window_is_timezone_aware_and_inclusive(tmp_path):
    source = tmp_path / "export.csv"
    write_csv(
        source,
        [
            row(timestamp="2026-07-29 10:00:20"),
            row(timestamp="2026-07-29 11:01:56"),
            row(timestamp="2026-07-29 11:01:57"),
        ],
    )
    accepted, _, _ = HistoricalUatCsvAdapter(source).read()
    selected = within_window(
        accepted,
        datetime.fromisoformat("2026-07-29T10:00:20+08:00"),
        datetime.fromisoformat("2026-07-29T11:01:56+08:00"),
    )
    assert [item.row_number for item in selected] == [2, 3]


def test_reconciliation_matches_only_a_unique_full_signature(tmp_path):
    source = tmp_path / "export.csv"
    write_csv(source, [row(), row(timestamp="2026-07-29 10:30:00")])
    csv_rows, _, _ = HistoricalUatCsvAdapter(source).read()
    result = reconcile([v3()], csv_rows)
    assert result["status"] == "matched"
    assert result["matched_count"] == 1
    assert result["matched"][0]["csv_row_number"] == 2
    assert result["extra_count"] == 1
    assert result["duplicate_association"] is False


def test_reconciliation_rejects_ambiguity_instead_of_nearest_matching(tmp_path):
    source = tmp_path / "export.csv"
    write_csv(
        source,
        [
            row(timestamp="2026-07-29 10:10:22"),
            row(timestamp="2026-07-29 10:10:23"),
        ],
    )
    csv_rows, _, _ = HistoricalUatCsvAdapter(source).read()
    result = reconcile([v3()], csv_rows)
    assert result["status"] == "pending"
    assert result["matched_count"] == 0
    assert result["ambiguous_count"] == 1
    with pytest.raises(ValueError, match="reconciliation_not_complete"):
        project_metric_events([v3()], csv_rows, result)


def test_one_csv_row_cannot_match_two_v3_records(tmp_path):
    source = tmp_path / "export.csv"
    write_csv(source, [row()])
    csv_rows, _, _ = HistoricalUatCsvAdapter(source).read()
    result = reconcile([v3("UR-V3-001"), v3("UR-V3-002")], csv_rows)
    assert result["matched_count"] == 0
    assert result["ambiguous_count"] == 2


def test_metric_projection_has_traceable_safe_provenance_and_no_channel_guess(tmp_path):
    source = tmp_path / "export.csv"
    write_csv(source, [row()])
    csv_rows, _, _ = HistoricalUatCsvAdapter(source).read()
    source_v3 = v3()
    result = reconcile([source_v3], csv_rows)
    events = project_metric_events([source_v3], csv_rows, result)
    event = events[0]
    assert event["actual_channel"] is None
    assert event["reconciliation_provenance"] == {
        "plan_id": "UR-V3-001",
        "execution_id": "EXEC-UR-V3-001",
        "csv_row_number": 2,
    }
    serialized = json.dumps(event)
    assert "credential-placeholder" not in serialized
    assert "203.0.113.42" not in serialized
