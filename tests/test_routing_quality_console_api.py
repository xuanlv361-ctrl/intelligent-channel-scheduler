import hashlib
import importlib
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import backend.app as application
from tests.enterprise_test_client import authenticated_test_client
app=application.app
client=authenticated_test_client(app,runtime=application.ENTERPRISE_HTTP,roles=("operations_admin",))
def test_versioned_routes_and_schemas():
 for path in ["/api/v1/system/status","/api/v1/metrics/snapshots","/api/v1/statistical-confidence/snapshots","/api/v1/overview","/api/v1/health/channels","/api/v1/shadow/summary","/api/v1/shadow/decisions","/api/v1/errors","/api/v1/budgets/summary","/api/v1/mappings","/api/v1/compatibility","/api/v1/exports"]:
  response=client.get(path);assert response.status_code==200,path
 assert client.get("/api/v1/system/status").json()["external_execution_enabled"] is False
 readiness=client.get("/api/v1/system/readiness")
 assert readiness.status_code==200 and readiness.json()["status"]=="ready"
 assert readiness.json()["network_checked"] is False
 dependencies=client.get("/api/v1/system/dependencies").json()
 assert {item["dependency"] for item in dependencies["dependencies"]}>={
  "sqlite","price_catalog","domestic_uat_session"}
 assert dependencies["credentials_exposed"] is False


def test_real_error_center_projects_standardized_call_log_failures(monkeypatch):
 class FakeCallLogs:
  def list_records(self,**_kwargs):
   return {"updated_at":"2026-08-05T10:00:00+00:00","items":[{
    "record_id":"CALL-1","request_id":"REQ-1","decision_id":"DEC-1",
    "channel_id":None,"requested_model":"model-a","error_category":"timeout",
    "error_code":"uat_transport_failed","request_status":"FAILED","http_status":None,
    "retryable":1,"is_historical":False,"occurred_at":"2026-08-05T09:59:00+00:00",
    "source_type":"realtime_execution",
   },{
    "record_id":"CALL-2","request_status":"SUCCESS",
   }]}
 monkeypatch.setattr(application,"current_call_logs",lambda _request=None:FakeCallLogs())
 response=client.get("/api/v1/errors?mode=uat&environment_id=china_uat")
 assert response.status_code==200
 payload=response.json()
 assert payload["source_type"]=="standardized_call_logs"
 assert payload["is_mock"] is False and payload["sample_size"]==1
 assert payload["items"][0]["request_id"]=="REQ-1"
 assert payload["items"][0]["error_category"]=="timeout"
 assert "body" not in payload["items"][0] and "authorization" not in str(payload).lower()


def test_same_origin_root_health_readiness_and_runtime_config():
 health=client.get("/health")
 assert health.status_code==200 and health.json()["status"]=="ok"
 ready=client.get("/ready")
 assert ready.status_code==200 and ready.json()["status"]=="ready"
 assert ready.json()["migration_status"]!="failed"
 runtime=client.get("/runtime-config.json")
 assert runtime.status_code==200
 assert runtime.json()=={
  "api_base_url":"","api_prefix":"/api","health_url":"/health",
  "ready_url":"/ready","deployment_mode":"local_operator",
  "credentials_exposed":False}


def test_same_origin_spa_fallback_and_path_traversal_are_safe():
 response=client.get("/collector")
 assert response.status_code in {200,503}
 assert "text/html" in response.headers["content-type"]
 assert "script-src 'self'" in response.headers["content-security-policy"]
 assert "object-src 'none'" in response.headers["content-security-policy"]
 traversal=client.get("/%2e%2e/config/enterprise_identity_v1.json")
 assert traversal.status_code in {404,503}
 assert "client_secret" not in traversal.text


def test_incremental_metrics_snapshot_contract_and_validation():
 response=client.get("/api/v1/metrics/snapshots?window=5m&limit=1")
 assert response.status_code==200
 assert {"aggregation_version","source_watermark","event_count","total",
         "limit","offset","has_more","items","refresh_runtime",
         "source_contract","network_called"}<=set(response.json())
 rejected=client.get("/api/v1/metrics/snapshots?window=7m")
 assert rejected.status_code==400
 assert rejected.json()["detail"]["code"]=="invalid_metric_window"


def test_statistical_confidence_contract_is_versioned_and_network_free():
 response=client.get("/api/v1/statistical-confidence/snapshots?limit=1")
 assert response.status_code==200
 payload=response.json()
 assert payload["confidence_version"]=="weighted_wilson_v1"
 assert payload["policy_version"]=="statistical_confidence_policy_v1"
 assert payload["network_called"] is False
 assert payload["source_contract"]=="normalized_local_metric_evidence_projection"


def test_scheduler_reconstruction_overhead_and_price_status_are_safe(monkeypatch):
 module=importlib.import_module("backend.app")
 class FakeReconstruction:
  def reconstruct(self,decision_id):
   return {"status":"ready","decision_id":decision_id,
           "reconstruction_sha256":"A"*64,"network_called":False}
 class FakeOverhead:
  def status(self,*,window_minutes):
   return {"status":"ready","window_minutes":window_minutes,
           "provider_latency_included":False,"network_called":False}
 class FakePrices:
  policy={"policy_version":"price_sync_policy_v1"}
  transport=None
  def catalog_status(self,source_id):
   return {"status":"unavailable","source_id":source_id,
           "catalog_version":None,"network_called":False}
  def audit(self,limit): return []
 class FakePriceSchedule:
  def status(self,source_id):
   return {"source_id":source_id,"enabled":False,
           "state":"never_configured","network_called":False}
 monkeypatch.setattr(module,"current_decision_reconstruction",lambda:FakeReconstruction())
 monkeypatch.setattr(module,"current_scheduler_overhead",lambda:FakeOverhead())
 monkeypatch.setattr(module,"current_price_catalog",lambda *_:FakePrices())
 monkeypatch.setattr(module,"current_price_sync_supervisor",lambda *_:FakePriceSchedule())
 reconstructed=client.get("/api/v1/scheduler/decisions/D-1/reconstruction")
 assert reconstructed.status_code==200
 assert reconstructed.json()["network_called"] is False
 overhead=client.get("/api/v1/scheduler/overhead?window_minutes=30").json()
 assert overhead["provider_latency_included"] is False
 prices=client.get("/api/v1/prices/status").json()
 assert prices["status"]=="unavailable"
 assert prices["automatic_sync_state"]=="never_configured"
 assert prices["source_adapter_configured"] is False
 assert client.get("/api/v1/prices/audit").json()["items"]==[]


def test_incremental_metrics_refresh_requires_origin_and_confirmation(monkeypatch):
 module=importlib.import_module("backend.app")
 class FakeAdapter:
  def synchronize_safely(self,*,rebuild=False):
   return {"status":"ready","rebuild":rebuild,"network_called":False,
           "protected_evidence_modified":False}
 monkeypatch.setattr(module,"current_metrics_adapter",lambda *_:FakeAdapter())
 missing_origin=client.raw_request("POST","/api/v1/metrics/refresh",json={
  "explicit_confirmation":True})
 assert missing_origin.status_code==403
 missing_confirmation=client.post(
  "/api/v1/metrics/refresh",
  headers={"Origin":"http://127.0.0.1:5174","Content-Type":"application/json",
           "Host":"127.0.0.1:8000"},
  json={"explicit_confirmation":False})
 assert missing_confirmation.status_code==400
 refreshed=client.post(
  "/api/v1/metrics/refresh",
  headers={"Origin":"http://127.0.0.1:5174","Content-Type":"application/json",
           "Host":"127.0.0.1:8000"},
  json={"explicit_confirmation":True,"rebuild":True})
 assert refreshed.status_code==200
 assert refreshed.json()=={"status":"ready","rebuild":True,
  "network_called":False,"protected_evidence_modified":False}
def test_import_preview_and_no_absolute_paths():
 response=client.post("/api/v1/imports/preview",files={"file":("sample.csv",b"request_id,channel_id,requested_model\nr1,19,m\n","text/csv")})
 assert response.status_code==200
 assert str(ROOT) not in response.text
def test_replay_bug_and_config_review():
 assert client.post("/api/v1/replay/run",json={"strategy":"fastest_first"}).json()["network_calls"]==0
 bug=client.post("/api/v1/bugs/generate",json={"request_id":"r","http_status":429,"message":"Bearer verysecretvalue"}).json()
 assert "verysecretvalue" not in str(bug)
 review=client.post("/api/v1/config-review",json={"weights":{"a":-1},"fallback":None,"timeout_ms":None}).json()
 assert review["status"] in {"ready","insufficient_data"}
 assert review["is_mock"] is False
 assert review["configuration"]["policy_version"]=="decision-policy-v2.0.0"
def test_export_generate_covers_every_listed_report_and_rejects_unknown_names():
 catalog=client.get("/api/v1/exports").json()["items"]
 for item in catalog:
  response=client.post("/api/v1/exports/generate",json={"name":item["name"],"mode":"demo","environment_id":"all"})
  assert response.status_code==200,item["name"]
  body=response.json()
  assert body["format"]==item["format"] and body["name"]==item["name"]
  assert len(body["sha256"])==64 and body["content"]
  assert body["sha256"]==hashlib.sha256(body["content"].encode("utf-8")).hexdigest()
 rejected=client.post("/api/v1/exports/generate",json={"name":"不存在的报告"})
 assert rejected.status_code==404 and rejected.json()["detail"]["code"]=="UNKNOWN_REPORT"
def test_export_generate_content_is_stable_aside_from_the_timestamp_and_never_leaks_secrets():
 first=client.post("/api/v1/exports/generate",json={"name":"错误报告","mode":"demo"}).json()
 second=client.post("/api/v1/exports/generate",json={"name":"错误报告","mode":"demo"}).json()
 strip_timestamp=lambda text:"\n".join(line for line in text.splitlines() if not line.startswith("生成时间："))
 assert strip_timestamp(first["content"])==strip_timestamp(second["content"])
 assert str(ROOT) not in first["content"] and "Authorization" not in first["content"]
def test_structured_not_found_error():
 response=client.get("/api/v1/health/channels/missing")
 assert response.status_code==404 and response.json()["detail"]["code"]=="NOT_FOUND"
def test_cost_contract_is_utf8_and_decimal_safe():
 response=client.get("/api/v1/budgets/summary?mode=demo")
 assert response.headers["content-type"].startswith("application/json")
 assert response.encoding=="utf-8"
 assert response.json()["provenance"]["label"]=="演示数据"
 assert response.json()["summary"]["total_spend"]=="0.1150"
 assert "0.11499999999999999" not in response.text
 assert "玫瑰三号" in response.text


def test_empty_overseas_budget_is_not_relabelled_as_demo():
 response=client.get("/api/v1/budgets/summary?mode=uat&environment_id=overseas")
 assert response.status_code==200
 payload=response.json()
 assert payload["mode"]=="uat" and payload["environment_id"]=="overseas"
 assert payload["sample_size"]==0 and payload["source_type"] is None
 assert payload["summary"]["currency"] is None and payload["currency"] is None
 assert "币种待确认" in payload["limitations"][0]


def test_analytical_api_provenance_contract_is_explicit():
 required={"mode","environment_id","source_type","is_mock","sample_size",
           "evidence_id","updated_at","configuration_version","currency","limitations"}
 for path in ("/api/v1/overview","/api/v1/health/channels","/api/v1/errors",
              "/api/v1/budgets/summary","/api/v1/mappings",
              "/api/v1/compatibility","/api/v1/exports"):
  response=client.get(path)
  assert response.status_code==200,path
  assert required<=set(response.json()),path
 for path,body in (("/api/v1/replay/run",{}),):
  response=client.post(path,json=body)
  assert response.status_code==200,path
  assert required<=set(response.json()),path
 review=client.get("/api/v1/config-review")
 assert review.status_code==200
 assert {"data_source","configuration","metrics","risks","impact","rollback","evidence"}<=set(review.json())
 assert review.json()["is_mock"] is False


def test_realtime_and_persistent_subrouters_are_present_in_final_application():
 paths=set(app.openapi()["paths"])
 required={
  "/api/v1/log-sync/health",
  "/api/v1/log-sync/jobs",
   "/api/v1/log-sync/persistent/status",
   "/api/v1/log-sync/persistent/pairing/start",
   "/api/v1/log-sync/persistent/reauthentication/start",
   "/api/v1/environments/china_uat/log-page/status",
   "/api/v1/environments/china_uat/log-page/preview",
   "/api/v1/environments/china_uat/log-page/confirm",
   "/api/v1/collector/import/preview",
 }
 assert required<=paths
 assert client.get("/api/v1/log-sync/health").status_code==200
 assert client.get("/api/v1/log-sync/persistent/status").status_code==200
