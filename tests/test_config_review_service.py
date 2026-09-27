from decimal import Decimal
from pathlib import Path

from backend.call_log_service import CallLogService
from backend.config_review_service import ConfigReviewService

ROOT = Path(__file__).resolve().parents[1]


def test_empty_database_never_generates_mock_conclusions(tmp_path):
    logs = CallLogService(tmp_path / "review.sqlite3")
    result = ConfigReviewService(logs.path, ROOT / "config").review()

    assert result["status"] == "insufficient_data"
    assert result["empty_message"] == "暂无真实日志，无法完成评审。"
    assert result["data_source"]["sample_count"] == 0
    assert result["metrics"]["success_rate"] is None
    assert result["metrics"]["p95_latency_ms"] is None
    assert result["metrics"]["average_cost"] is None
    assert result["risks"] == []
    assert result["impact"]["status"] == "unavailable"
    assert result["rollback"]["status"] == "unavailable"
    assert result["is_mock"] is False


def _real_call(logs: CallLogService, request_id: str, decision_id: str,
               channel: str, latency: float, cost: str, version: str) -> None:
    record_id = logs.start_execution(
        request_id=request_id, decision_id=decision_id, environment_id="china_uat",
        requested_model="deepseek-v4-flash", stream=False, channel_id=channel,
        channel_name=channel, provider="domestic-uat", configuration_version=version,
        metric_snapshot_id=f"snapshot-{version}",
    )
    logs.finish_execution(
        record_id, status="SUCCESS", response_id=f"RESP-{request_id}",
        actual_model="deepseek-v4-flash", http_status=200,
        total_latency_ms=latency, input_tokens=20, output_tokens=10,
        cost_amount=Decimal(cost), currency="CNY", channel_id=channel,
        channel_name=channel, provider="domestic-uat",
    )
    persisted = logs.list_records(limit=1)["items"][0]
    assert persisted["configuration_version"] == version
    assert persisted["metric_snapshot_id"] == f"snapshot-{version}"


def test_real_review_uses_logs_versions_and_drilldown_ids(tmp_path):
    logs = CallLogService(tmp_path / "review.sqlite3")
    _real_call(logs, "REQ-OLD-1", "DEC-OLD-1", "channel-a", 900, "0.010", "policy-old")
    _real_call(logs, "REQ-OLD-2", "DEC-OLD-2", "channel-b", 1100, "0.014", "policy-old")
    _real_call(logs, "REQ-NEW-1", "DEC-NEW-1", "channel-a", 1300, "0.018", "policy-new")
    _real_call(logs, "REQ-NEW-2", "DEC-NEW-2", "channel-a", 1500, "0.022", "policy-new")

    result = ConfigReviewService(logs.path, ROOT / "config").review()

    assert result["status"] == "ready"
    assert result["data_source"]["sample_count"] == 4
    assert result["data_source"]["sources"] == [{"source": "realtime_execution", "count": 4}]
    assert result["metrics"]["success_rate"] == 1.0
    assert result["metrics"]["p95_latency_ms"] > 0
    assert Decimal(result["metrics"]["average_cost"]) > 0
    assert result["metrics"]["concentration"]["channel_count"] == 2
    assert result["impact"]["status"] == "unavailable"
    assert "真实调用证据" in result["impact"]["basis"]
    assert {item["request_id"] for item in result["evidence"]} >= {"REQ-OLD-1", "REQ-NEW-1"}
    assert all(item["decision_id"] for item in result["evidence"])
    assert result["rollback"]["status"] == "unavailable"


def test_real_configuration_versions_are_immutable_and_rollback_creates_a_new_version(tmp_path):
    logs = CallLogService(tmp_path / "review.sqlite3")
    service = ConfigReviewService(logs.path, ROOT / "config")
    baseline = service.versions()[0]

    changed = service.create_version(
        base_version=baseline["configuration_version"],
        patch={"timeout": 31}, created_by="test-operator",
        change_reason="本地非生产超时验证",
    )
    assert changed["previous_version"] == baseline["configuration_version"]
    assert changed["diff"]["timeout"]["before"] != 31
    assert service.versions()[-1]["checksum"] == baseline["checksum"]

    rolled_back = service.rollback(
        target_version=baseline["configuration_version"],
        active_version=changed["configuration_version"],
        created_by="test-operator", change_reason="验证不可变回滚",
    )
    assert rolled_back["rollback_target"] == baseline["configuration_version"]
    assert rolled_back["payload"]["timeout"] == baseline["payload"]["timeout"]
    assert len(service.versions()) == 3


def test_exact_duplicate_is_linked_and_excluded_from_review_statistics(tmp_path):
    logs = CallLogService(tmp_path / "review.sqlite3")
    _real_call(logs, "REQ-DUP", "DEC-DUP", "channel-a", 900, "0.010", "policy-live")
    with logs.connect() as db:
        source = dict(db.execute("SELECT * FROM standardized_call_logs WHERE request_id='REQ-DUP'").fetchone())
        source.update(record_id="COPY-DUP", cursor_id=None, source_type="historical_uat_csv",
                      request_id=None, response_id=None, decision_id=None, channel_id=None,
                      channel_name=None, provider=None, evidence_scope="model_usage_only",
                      is_historical=1, source_record_id="COPY-DUP", evidence_level="historical_statistics")
        columns = [row[1] for row in db.execute("PRAGMA table_info(standardized_call_logs)") if row[1] != "cursor_id"]
        db.execute(f"INSERT INTO standardized_call_logs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                   [source.get(column) for column in columns])
    counts = logs.reconcile_duplicates()
    result = ConfigReviewService(logs.path, ROOT / "config").review()
    assert counts["exact_duplicate_count"] == 1
    assert result["data_source"]["raw_count"] == 2
    assert result["data_source"]["deduplicated_count"] == 1
    assert result["metrics"]["sample_count"] == 1


def test_historical_rows_without_channel_do_not_create_channel_concentration(tmp_path):
    logs = CallLogService(tmp_path / "review.sqlite3")
    with logs.connect() as db:
        db.execute("""INSERT INTO standardized_call_logs(
          record_id,occurred_at,environment_id,requested_model,actual_model,stream,
          request_status,attempt_number,total_attempts,total_latency_ms,input_tokens,
          output_tokens,cost_amount,currency,source_type,evidence_scope,is_historical,
          created_at,updated_at,tenant_id,workspace_id)
          VALUES('HIST-1','2026-07-29T02:00:00+00:00','china_uat','model-a','model-a',0,
          'SUCCESS',1,1,800,10,5,'0.003','CNY','historical_uat_csv','model_usage_only',1,
          '2026-07-29T02:00:00+00:00','2026-07-29T02:00:00+00:00',?,?)""",
          logs.scope.sql_parameters())

    result = ConfigReviewService(logs.path, ROOT / "config").review()
    assert result["status"] == "ready"
    assert result["metrics"]["concentration"]["hhi"] is None
    assert result["metrics"]["concentration"]["sample_count"] == 0
    assert result["evidence"] == []
