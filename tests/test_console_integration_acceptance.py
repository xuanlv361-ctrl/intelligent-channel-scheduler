import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"src"))
import backend.app as backend
from services.console_service import Store,import_preview
from tests.enterprise_test_client import authenticated_test_client
FIX=ROOT/"tests"/"fixtures"/"integration"

def upload(client,name,endpoint="preview"):
 data=(FIX/name).read_bytes()
 media="application/json" if name.endswith(("json","jsonl")) else "text/csv"
 return client.post(f"/api/v1/imports/{endpoint}",files={"file":(name,data,media)},data={"source_type":"integration_test_fixture","mapping":"{}"})
def test_all_acceptance_fixtures_are_explicitly_labeled():
 for path in FIX.iterdir():
  if path.name=="malformed.json":continue
  assert "integration_test_fixture" in path.read_text(encoding="utf-8")
def test_fixture_issue_catalog_and_redaction():
 expected={"missing_channel_id.csv":"missing_channel_id","missing_request_id.csv":"missing_request_id",
  "model_mismatch.csv":"model_mismatch","duplicate_record.csv":"duplicate_request_id",
  "negative_tokens.csv":"negative_input_tokens","cost_mismatch.csv":"cost_mismatch",
  "invalid_timestamp.csv":"invalid_timestamp","latency_outlier.csv":"latency_outlier",
  "authorization_redaction.csv":"sensitive_data","api_key_redaction.csv":"sensitive_data",
  "formula_injection.csv":"csv_formula_injection"}
 for filename,code in expected.items():
  report=import_preview(filename,(FIX/filename).read_bytes(),source_type="integration_test_fixture")
  assert code in {item["code"] for item in report["issues"]},filename
  serialized=json.dumps(report)
  assert "integration-fixture-not-a-real-token" not in serialized
  assert "integrationFixtureValue123456" not in serialized
def test_real_local_contract_confirm_persistence_and_downstream(tmp_path,monkeypatch):
 monkeypatch.setattr(backend,"STORE",Store(tmp_path/"isolated.sqlite3"))
 client=authenticated_test_client(backend.app,runtime=backend.ENTERPRISE_HTTP,roles=("operations_admin",))
 preview=upload(client,"valid.csv").json()
 assert preview["row_count"]==2 and preview["detected_columns"]
 validated=upload(client,"valid.csv","validate").json()
 assert validated["valid_count"]==2
 confirmed=upload(client,"valid.csv","confirm").json()
 assert confirmed["audit_status"]=="immutable_audited"
 assert client.get("/api/v1/imports").json()["items"][0]["batch_id"]==confirmed["batch_id"]
 assert client.get("/api/v1/overview?mode=uat").json()["records"]==2
 cards=client.get("/api/v1/health/channels?mode=uat").json()["items"]
 assert cards[0]["sample_size"]==2 and cards[0]["health_score"] is None
 for path in ("budgets/summary","mappings","compatibility","exports"):
  response=client.get(f"/api/v1/{path}?mode=uat")
  assert response.status_code==200 and response.json()["source_type"]=="integration_test_fixture",path
 errors=client.get("/api/v1/errors?mode=uat")
 assert errors.status_code==200
 assert errors.json()["source_type"]=="standardized_call_logs"
 shadow=client.get("/api/v1/shadow/summary?mode=uat").json()
 assert shadow["sample_size"]==0 and "缺少" in shadow["limitation"]
 reopened=Store(tmp_path/"isolated.sqlite3")
 assert reopened.batches()[0]["batch_id"]==confirmed["batch_id"]
def test_rejected_rows_do_not_enter_downstream(tmp_path,monkeypatch):
 monkeypatch.setattr(backend,"STORE",Store(tmp_path/"isolated.sqlite3"));client=authenticated_test_client(backend.app,runtime=backend.ENTERPRISE_HTTP,roles=("operations_admin",))
 result=upload(client,"negative_tokens.csv","confirm").json()
 assert result["rejected_count"]==1
 assert client.get("/api/v1/overview?mode=uat").json()["records"]==0
def test_malformed_json_returns_structured_error(tmp_path,monkeypatch):
 monkeypatch.setattr(backend,"STORE",Store(tmp_path/"isolated.sqlite3"));client=authenticated_test_client(backend.app,runtime=backend.ENTERPRISE_HTTP,roles=("operations_admin",))
 response=upload(client,"malformed.json")
 assert response.status_code==400 and response.json()["detail"]["code"]=="IMPORT_INVALID"
