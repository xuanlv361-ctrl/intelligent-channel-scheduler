from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

from backend.call_log_service import CallLogService
from backend.historical_replay_service import HistoricalReplayService

CSV_PATH = Path(r"E:\lx\调用日志_20260730_172539.csv")


def _call(logs: CallLogService, index: int, channel: str = "channel-a",
          latency: float = 100.0, cost: str | None = "0.10") -> str:
    request_id = f"REQ-{index:03d}"
    record_id = logs.start_execution(
        request_id=request_id, decision_id=f"DEC-{index:03d}",
        environment_id="china_uat", requested_model="model-a", stream=False,
        channel_id=channel, channel_name=channel,
        configuration_version="policy-v1", metric_snapshot_id="METRIC-v1")
    logs.finish_execution(record_id, status="SUCCESS", response_id=f"RESP-{index:03d}",
        actual_model="model-a", http_status=200, total_latency_ms=latency,
        input_tokens=100, cached_input_tokens=0, output_tokens=50,
        cost_amount=cost, currency="CNY", channel_id=channel, channel_name=channel)
    return request_id


def test_real_source_only_and_persisted_request_drilldown(tmp_path: Path):
    path = tmp_path / "replay.sqlite3"
    logs = CallLogService(path)
    for index in range(1, 4):
        _call(logs, index, latency=100 + index)
    service = HistoricalReplayService(path)

    source = service.source_summary({"environment_id": "china_uat"})
    assert source["sample_count"] == 3
    assert source["exact_association_count"] == 3
    assert source["is_mock"] is False
    assert source["sources"] == [{"source_type": "realtime_execution", "count": 3}]

    run = service.run(baseline_strategy="actual_observed",
                      candidate_strategy="latency_first")
    assert run["network_calls"] == 0
    assert run["sample_count"] == 3
    assert run["items"][0]["request_id"].startswith("REQ-")
    assert run["items"][0]["timeline"][0]["title"] == "输入请求摘要"
    assert run["sla"]["coverage_count"] == 3
    assert run["sla"]["candidate_risk"] is None
    assert run["risk_count"] == 0
    restored = service.get(run["replay_id"])
    assert restored["items"][0]["decision_id"].startswith("DEC-")


def test_missing_price_is_not_zero_and_new_real_log_enters_next_run(tmp_path: Path):
    path = tmp_path / "replay.sqlite3"
    logs = CallLogService(path)
    for index in range(1, 4):
        _call(logs, index)
    service = HistoricalReplayService(path)
    first = service.run(baseline_strategy="actual_observed",
                        candidate_strategy="latency_first")
    item = first["items"][0]["counterfactual"]
    assert item["estimated_cost"] is None
    assert item["cost_unavailable_reason"] == "price_version_missing"
    assert first["cost"]["candidate"] is None

    _call(logs, 4)
    second = service.run(baseline_strategy="actual_observed",
                         candidate_strategy="latency_first")
    assert second["sample_count"] == first["sample_count"] + 1


def test_empty_source_and_csv_export_keep_missing_values_empty(tmp_path: Path):
    path = tmp_path / "replay.sqlite3"
    CallLogService(path)
    service = HistoricalReplayService(path)
    source = service.source_summary()
    assert source["sample_count"] == 0
    assert source["empty_message"] == "当前没有可用于回放的真实UAT日志。"

    logs = CallLogService(path)
    for index in range(1, 4):
        _call(logs, index, cost=None)
    run = service.run(baseline_strategy="actual_observed",
                      candidate_strategy="latency_first")
    exported = list(csv.DictReader(io.StringIO(service.export_csv(run["replay_id"]))))
    assert len(exported) == 3
    assert exported[0]["estimated_cost"] == ""
    assert exported[0]["actual_cost"] == ""
    assert exported[0]["request_id"].startswith("REQ-")


def test_historical_rows_without_ids_remain_valid_historical_statistics(tmp_path: Path):
    path = tmp_path / "replay.sqlite3"
    logs = CallLogService(path)
    logs.import_normalized_rows([{
        "timestamp": "2026-07-29T02:00:20Z", "requested_model": "model-a",
        "actual_model": "model-a", "request_status": "SUCCESS", "http_status": 200,
        "stream": False, "latency_ms": 200, "input_tokens": 10,
        "output_tokens": 20, "cost_cny": "0.01",
    }], environment_id="china_uat", import_batch_id="batch-1",
       source_type="historical_uat_csv")
    service = HistoricalReplayService(path)
    run = service.run(baseline_strategy="actual_observed",
                      candidate_strategy="latency_first")
    assert run["exact_association_count"] == 0
    assert run["unlinked_count"] == 1
    assert run["historical_statistics_count"] == 1
    assert run["items"][0]["association"] == "historical_statistics"
    assert run["actual"]["input_tokens"] == 10
    assert run["actual"]["output_tokens"] == 20
    assert run["actual"]["total_cost"] == "0.01"
    assert run["cost"]["candidate"] is None
    assert run["cost"]["candidate_unavailable_reason"] == "historical_request_content_missing"
    assert run["items"][0]["counterfactual"]["channel_id"] is None


def test_sha_pinned_csv_actual_statistics_are_independent_from_candidate_estimate(
        tmp_path: Path):
    logs = CallLogService(tmp_path / "replay.sqlite3")
    imported = logs.initialize_historical_csv(CSV_PATH)
    assert imported["source_row_count"] == 646
    service = HistoricalReplayService(tmp_path / "replay.sqlite3")

    source = service.source_summary({"environment_id": "china_uat"})
    assert source["actual"]["record_count"] == 646
    assert source["actual"]["input_tokens"] == 9453
    assert source["actual"]["output_tokens"] == 68318
    assert source["actual"]["total_cost"] == "13.256422"
    assert source["actual"]["p95_latency_ms"] == 24000.0
    assert source["actual"]["p99_latency_ms"] == 43600.0
    assert source["actual"]["failure_count"] == 1
    assert source["actual"]["stream_count"] == 15
    assert source["actual"]["nonstream_count"] == 631
    assert source["actual"]["model_count"] == 9
    assert source["evidence_capabilities"]["historical_statistics_only"] == 646

    run = service.run(baseline_strategy="actual_observed",
                      candidate_strategy="latency_first", limit=1000)
    assert run["historical_statistics_count"] == 646
    assert run["exact_association_count"] == 0
    assert run["actual"]["total_cost"] == "13.256422"
    assert run["cost"]["baseline"] == "13.256422"
    assert run["cost"]["candidate"] is None
    assert run["cost"]["candidate_unavailable_reason"] == "historical_request_content_missing"
    assert run["choice_comparable_count"] == 0
