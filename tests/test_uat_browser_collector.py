import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from collector import ALLOWED_HOSTS,SOURCE_TYPE
from collector.evidence_writer import EvidenceWriter
from collector.log_page_parser import in_date_range,normalize_record,parse_dom_table,parse_structured
from collector.network_response_capture import HostNotAllowed,capture_json_response,require_allowed_url,safe_source_url
from collector.pagination_controller import PaginationController,PaginationLimits
from collector.uat_browser_collector import CollectorConfig
from backend.browser_import_service import BrowserImportService
from backend.environment_runtime_settings import EnvironmentRuntimeSettings
from backend.collector_routes import build_collector_router
from backend.uat_service import UatStore
from src.services.console_service import Store,uat_records

def test_allowlist_accepts_only_exact_uat_hosts():
    assert require_allowed_url("https://uat.weimeta.cn/logs")
    assert require_allowed_url("https://admin-uat.weimeta.cn/audit")
    for url in ("https://weimeta.cn/logs","https://admin.weimeta.cn/logs","https://evil.example/logs",
                "http://uat.weimeta.cn/logs","https://uat.weimeta.cn.evil.example/logs"):
        with pytest.raises(HostNotAllowed):require_allowed_url(url)

def test_sensitive_query_is_removed_and_headers_are_never_captured():
    assert safe_source_url("https://uat.weimeta.cn/logs?token=secret&page=1")=="https://uat.weimeta.cn/logs"
    item=capture_json_response("https://uat.weimeta.cn/logs?token=x","application/json",b'{"items":[]}')
    assert item and not hasattr(item,"headers") and "token" not in item.source_url

def test_structured_json_redacts_secrets_and_keeps_missing_fields_null():
    rows=parse_structured({"data":{"items":[{"request_id":"r1","requested_model":"m","api_key":"sk-abcdefgh",
      "authorization":"Bearer abcdefgh","prompt":"private","ip":"10.2.3.4"}]}},"https://uat.weimeta.cn/logs","2026-07-27T00:00:00Z")
    assert len(rows)==1 and rows[0]["request_id"]=="r1"
    assert rows[0]["actual_model"] is None and rows[0]["channel_id"] is None and rows[0]["http_status"] is None
    assert "sk-abcdefgh" not in json.dumps(rows[0]) and "private" not in json.dumps(rows[0])
    assert rows[0]["source_type"]==SOURCE_TYPE and rows[0]["environment"]=="uat" and rows[0]["is_mock"] is False

def test_dom_table_fallback_maps_visible_columns():
    html="<table><tr><th>请求</th><th>渠道</th><th>状态码</th></tr><tr><td>r1</td><td>19</td><td>200</td></tr></table>"
    rows=parse_dom_table(html,{"请求":"request_id","渠道":"channel_id","状态码":"http_status"},
                         "https://admin-uat.weimeta.cn/logs","2026-07-27T00:00:00Z")
    assert rows[0]["request_id"]=="r1" and rows[0]["channel_id"]=="19" and rows[0]["http_status"]=="200"
    assert rows[0]["channel_name"] is None

def test_date_range_filtering_is_inclusive_and_invalid_dates_are_rejected():
    assert in_date_range({"logged_at":"2026-07-27T12:00:00Z"},"2026-07-27","2026-07-27")
    assert not in_date_range({"logged_at":"2026-07-26T23:59:59Z"},"2026-07-27","2026-07-28")
    assert not in_date_range({"logged_at":"not-a-date"},"2026-07-01","2026-07-31")

def test_pagination_limits_duplicate_detection_and_checkpoint_resume(tmp_path):
    checkpoint=tmp_path/"checkpoint.json";pager=PaginationController(PaginationLimits(2,3,250),checkpoint)
    assert pager.should_continue(0,0) and not pager.should_continue(2,0) and not pager.should_continue(0,3)
    row={"request_id":"r1","logged_at":"2026-07-27T00:00:00Z","raw_record_sha256":"a"}
    assert pager.unique(row) and not pager.unique(row)
    pager.save_checkpoint(1,row,"2026-07-27T01:00:00Z")
    assert pager.load_checkpoint()["last_request_id"]=="r1"
    assert "cookie" not in checkpoint.read_text().lower()

def test_evidence_is_append_only_and_redacted(tmp_path):
    writer=EvidenceWriter(tmp_path)
    first=writer.write("COL-X","https://uat.weimeta.cn/logs?signature=x","logs","application/json",
                       {"api_key":"sk-abcdefgh","cookie":"secret","items":[{"request_id":"r1"}]},1)
    saved=Path(first["evidence_path"]).read_text(encoding="utf-8")
    assert "sk-abcdefgh" not in saved and "secret" not in saved and "signature" not in saved
    assert first["raw_payload_sha256"] in saved

def test_collector_config_is_manual_and_has_conservative_defaults():
    config=CollectorConfig("https://uat.weimeta.cn",["https://uat.weimeta.cn/logs"])
    assert config.limits.maximum_pages==10 and config.limits.maximum_records==500 and config.limits.delay_ms==1500
    source=Path("collector/uat_browser_collector.py").read_text(encoding="utf-8")
    assert "headless=False" in source and "input(" in source and "storage_state" not in source.split("# deliberately")[0]
    assert "context.close()" in source

def _service(tmp_path):
    db=tmp_path/"collector.sqlite3";store=Store(db);return BrowserImportService(db,store),store

def test_preview_rejects_unknown_hosts_and_deduplicates(tmp_path):
    service,_=_service(tmp_path)
    result=service.preview({"collection_id":"COL-1","environment":"uat","source_type":SOURCE_TYPE,"records":[
      {"request_id":"r1","source_url":"https://uat.weimeta.cn/logs"},
      {"request_id":"r1","source_url":"https://uat.weimeta.cn/logs"},
      {"request_id":"r2","source_url":"https://weimeta.cn/logs"}]})
    assert result["record_count"]==1 and result["duplicate_count"]==1 and result["rejected_count"]==1
    assert result["cookies_stored"]==0 and result["credentials_stored"]==0

def test_confirm_reuses_import_pipeline_and_is_idempotent(tmp_path):
    service,store=_service(tmp_path)
    preview=service.preview({"collection_id":"COL-2","environment":"uat","source_type":SOURCE_TYPE,"records":[{
      "request_id":"r1","requested_model":"m","source_url":"https://uat.weimeta.cn/logs","logged_at":"2026-07-27T00:00:00Z"}]})
    first=service.confirm("COL-2",preview["payload_sha256"],True,"china_uat")
    second=service.confirm("COL-2",preview["payload_sha256"],True,"china_uat")
    assert first["idempotent"] is False and second["idempotent"] is True
    assert len(store.batches())==1 and len(first["downstream_refresh"])==8

def test_actual_channel_and_success_are_never_inferred_for_browser_records(tmp_path):
    service,store=_service(tmp_path)
    preview=service.preview({"collection_id":"COL-3","environment_id":"china_uat","records":[{
      "request_id":"r1","requested_model":"m","http_status":200,"source_url":"https://uat.weimeta.cn/logs"}]})
    service.confirm("COL-3",preview["payload_sha256"],True,"china_uat")
    row=uat_records(store)[0]
    assert row["channel_id"]=="" and row["channel_name"] is None and row["result"]=="unknown"

def test_production_actions_are_absent_from_collector():
    text="\n".join(Path("collector").joinpath(name).read_text(encoding="utf-8") for name in
                  ("uat_browser_collector.py","collector_cli.py","network_response_capture.py"))
    for token in ("create_api_key","delete_api_key","channel_weight","channel_priority","storage_state="):
        assert token not in text

def test_collector_api_status_preview_confirm_and_history(tmp_path):
    service,_=_service(tmp_path);app=FastAPI();app.include_router(build_collector_router(service,lambda:0));client=TestClient(app)
    status=client.get("/api/v1/collector/status").json()
    assert status["read_only"] is True and status["allowed_hosts"]==sorted(ALLOWED_HOSTS)
    assert status["environment_id"]=="china_uat"
    body={"collection_id":"COL-API","source_type":SOURCE_TYPE,"environment":"uat","records":[{
      "request_id":"r-api","requested_model":"m","source_url":"https://admin-uat.weimeta.cn/logs"}]}
    preview=client.post("/api/v1/collector/import/preview",json=body)
    assert preview.status_code==200
    value=preview.json()
    confirmed=client.post("/api/v1/collector/import/confirm",json={"collection_id":"COL-API","environment_id":"china_uat",
      "payload_sha256":value["payload_sha256"],"confirmed":True})
    assert confirmed.status_code==200 and confirmed.json()["downstream_refresh"]
    assert client.get("/api/v1/collector/collections",params={"environment_id":"china_uat"}).json()["items"][0]["status"]=="confirmed"

def test_overseas_collector_is_blocked_until_exact_log_page_is_confirmed(tmp_path):
    service,_=_service(tmp_path);app=FastAPI();app.include_router(build_collector_router(service,lambda:0));client=TestClient(app)
    status=client.get("/api/v1/collector/status",params={"environment_id":"overseas"})
    assert status.status_code==200 and status.json()["status"]=="blocked"
    assert status.json()["blocking_reason"]=="overseas_log_page_unconfirmed"
    assert status.json()["source_type"] is None
    preview=client.post("/api/v1/collector/import/preview",json={"environment_id":"overseas","records":[]})
    assert preview.status_code==400
    assert preview.json()["detail"]["message"]=="overseas_log_page_unconfirmed"

def test_request_id_deduplication_is_scoped_to_environment(tmp_path,monkeypatch):
    service,_=_service(tmp_path)
    china=service.preview({"collection_id":"COL-CN","environment_id":"china_uat","records":[{
      "request_id":"same","source_url":"https://uat.weimeta.cn/logs"}]})
    service.confirm("COL-CN",china["payload_sha256"],True,"china_uat")
    monkeypatch.setattr("backend.browser_import_service._environment",lambda environment_id:{
      "display_name":"overseas","logs_page_url":"https://weimeta.ai/operator-confirmed-log",
      "collector_allowed_hosts":["weimeta.ai"]})
    overseas=service.preview({"collection_id":"COL-OS","environment_id":"overseas",
      "source_type":"measured_overseas_browser_collector","records":[{
        "request_id":"same","source_url":"https://weimeta.ai/operator-confirmed-log"}]})
    assert overseas["record_count"]==1 and overseas["environment_id"]=="overseas"
    assert overseas["source_type"]=="measured_overseas_browser_collector"
    assert overseas["cookies_stored"]==0 and overseas["credentials_stored"]==0

def test_commands_and_history_are_strictly_environment_isolated(tmp_path):
    path=tmp_path/"isolated.sqlite3";UatStore(path)
    runtime=EnvironmentRuntimeSettings(path)
    observed=runtime.preview_log_page("https://weimeta.ai/console/log")
    runtime.confirm_log_page(observed["validation_id"],observed["value_sha256"],True,"tester")
    domestic=runtime.preview_environment_log_page(
      "china_uat","https://uat.weimeta.cn/console/billing/logs")
    runtime.confirm_environment_log_page(
      "china_uat",domestic["validation_id"],domestic["value_sha256"],True,"tester")
    store=Store(path);service=BrowserImportService(path,store,lambda:runtime)
    china=service.preview({"collection_id":"COL-CHINA","environment_id":"china_uat","records":[]})
    overseas=service.preview({"collection_id":"COL-OVERSEAS","environment_id":"overseas","records":[]})
    app=FastAPI();app.include_router(build_collector_router(service,lambda:0,lambda:runtime));client=TestClient(app)
    cn=client.get("/api/v1/collector/command-preview",params={"environment_id":"china_uat",
      "date_from":"2026-07-28","date_to":"2026-07-28"}).json()["command"]
    os=client.get("/api/v1/collector/command-preview",params={"environment_id":"overseas",
      "date_from":"2026-07-28","date_to":"2026-07-28"}).json()["command"]
    assert "https://uat.weimeta.cn" in cn and "weimeta.ai" not in cn.replace("uat.weimeta.cn","")
    assert "--environment-id china_uat" in cn and "measured_uat_browser_collector" in cn
    assert "https://weimeta.ai/console/log" in os and "api-uat" not in os and "uat.weimeta.cn" not in os
    assert "--environment-id overseas" in os and "measured_overseas_browser_collector" in os
    cn_history=client.get("/api/v1/collector/collections",params={"environment_id":"china_uat"}).json()["items"]
    os_history=client.get("/api/v1/collector/collections",params={"environment_id":"overseas"}).json()["items"]
    assert [x["collection_id"] for x in cn_history]==["COL-CHINA"]
    assert [x["collection_id"] for x in os_history]==["COL-OVERSEAS"]

@pytest.mark.parametrize("environment_id,console_url,log_page,source_type",[
  ("overseas","https://uat.weimeta.cn","https://weimeta.ai/console/log","measured_overseas_browser_collector"),
  ("china_uat","https://weimeta.ai","https://weimeta.ai/console/log","measured_uat_browser_collector"),
  ("overseas","https://weimeta.ai","https://weimeta.ai/console/log","measured_uat_browser_collector"),
])
def test_collector_config_rejects_cross_environment_mismatch(environment_id,console_url,log_page,source_type):
    with pytest.raises(ValueError):
        CollectorConfig(console_url,[log_page],environment_id=environment_id,source_type=source_type)
