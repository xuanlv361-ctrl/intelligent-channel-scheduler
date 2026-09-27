import json,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/"src"))
from services.console_service import *

def test_import_csv_quality_redaction_and_provenance():
 data=b"request_id,channel_id,requested_model,actual_model,http_status,input_tokens,output_tokens,total_tokens,stream,ttft_ms,note\nr1,19,m,m,200,2,3,5,FALSE,,ok\nr1,,m,x,999,-1,3,9,TRUE,,Authorization: Bearer secret-token\n"
 out=import_preview("sample.csv",data)
 assert out["row_count"]==2 and out["rejected_count"]==1
 assert len(out["source_sha256"])==64 and "Authorization" not in json.dumps(out["normalized_preview"])
 assert any(i["code"]=="duplicate_request_id" for i in out["issues"])
def test_json_jsonl_and_size_guards():
 assert len(parse_content("x.json",b'[{"request_id":"1"}]'))==1
 assert len(parse_content("x.jsonl",b'{"request_id":"1"}\n{"request_id":"2"}'))==2
 with pytest.raises(ValueError): parse_content("../x.exe",b"x")
def test_health_states_and_confidence():
 rows=demo_records(); cards=health(rows)
 assert {x["health_state"] for x in cards}>={"stale","insufficient_data"}
 assert all(0<=x["confidence"]<=1 and "sample_size" in x for x in cards)
def test_shadow_replay_determinism_boundaries():
 assert replay("fastest_first")==replay("fastest_first")
 assert replay()["network_calls"]==0 and replay()["execution_mode"]=="offline_estimate"
def test_all_17_error_categories_contract_and_retry_boundary():
 assert len(ERRORS)==17
 for category,(code,layer,retry,fallback,action) in ERRORS.items():
  assert code and layer and action
 assert classify(400,"bad")["maximum_total_attempts"]==2
 assert not classify(400,"bad")["retryable"]
 assert classify(429,"limited")["retryable"]
 assert "[REDACTED]" in classify(500,"Authorization: Bearer abcdefghijklmnop")["sanitized_message"]
def test_every_taxonomy_category_is_independently_reachable_by_classify():
 # Each fixture below is the (status, message, context) that documents how
 # classify() reaches this category; this is the "independent test per
 # category" gap the audit flagged (6 of 17 were previously unreachable).
 fixtures={
  "local_request_error": (None,"malformed json body",""),
  "user_parameter_error": (400,"missing field",""),
  "user_authentication_error": (401,"invalid key",""),
  "channel_authentication_error": (401,"invalid key","channel"),
  "model_not_supported": (404,"model not found",""),
  "rate_limited": (429,"too many requests",""),
  "upstream_timeout": (None,"upstream request timeout",""),
  "gateway_timeout": (504,"gateway timeout",""),
  "upstream_5xx": (502,"bad gateway",""),
  "protocol_error": (None,"protocol field conversion failed",""),
  "sse_incomplete": (None,"sse stream ended early",""),
  "response_schema_error": (None,"response schema invalid",""),
  "network_transport_error": (None,"connection reset",""),
  "budget_exceeded": (None,"daily budget exceeded",""),
  "execution_not_authorized": (None,"execution not_authorized","guard"),
  "all_candidates_unavailable": (None,"no_candidate eligible","routing"),
  "unknown_error": (None,"totally unclassified failure",""),
 }
 assert set(fixtures)==set(ERRORS)
 for category,(status,message,context) in fixtures.items():
  result=classify(status,message,context)
  assert result["error_category"]==category,f"{category} fixture resolved to {result['error_category']}"
def test_user_vs_upstream_401_boundary_is_explicit():
 assert classify(401,"invalid key")["error_category"]=="user_authentication_error"
 assert classify(401,"invalid key","channel")["error_category"]=="channel_authentication_error"
 assert classify(401,"invalid key")["retryable"] is False
 assert classify(401,"invalid key","channel")["fallback_allowed"] is True
def test_taxonomy_is_single_sourced_from_the_canonical_file():
 taxonomy=load_taxonomy()
 assert taxonomy["categories"].keys()==ERRORS.keys()
 for category,meta in taxonomy["categories"].items():
  code,layer,retry,fallback,action=ERRORS[category]
  assert (meta["error_code"],meta["error_layer"],meta["retryable"],meta["fallback_allowed"],meta["recommended_action"])==(code,layer,retry,fallback,action)
def test_bug_report_is_reproducible_and_sanitized():
 source={"request_id":"r1","http_status":429,"message":"api_key=supersecretvalue"}
 a=bug_report(source); b=bug_report(source)
 assert a["bug_id"]==b["bug_id"] and "supersecretvalue" not in a["markdown"]
def test_bug_report_fallback_trace_is_never_fabricated_without_a_decision_id():
 report=bug_report({"request_id":"r1","http_status":429,"message":"limited"})
 assert report["fallback_trace"]==[] and report["fallback_evidence_status"]=="no_decision_id_provided"
def test_bug_report_reports_unlinked_decision_id_honestly():
 report=bug_report({"request_id":"r1","http_status":429,"message":"limited","decision_id":"DEC-DOES-NOT-EXIST"})
 assert report["fallback_trace"]==[] and report["fallback_evidence_status"]=="decision_id_not_found_in_runtime_log"
def test_bug_report_pulls_real_fallback_trace_from_a_linked_decision(tmp_path,monkeypatch):
 log_path=tmp_path/"runtime.jsonl"
 DecisionLogger(log_path).log_runtime({"decision_id":"DEC-LINKED","runtime_version":"v1.1.0","request_id":"r1","mode":"mock_execute","strategy":"fastest_first","fallback_trace":["19","48"]})
 monkeypatch.setattr("services.console_service.DECISION_LOG_PATH",log_path)
 report=bug_report({"request_id":"r1","http_status":500,"message":"upstream failed","decision_id":"DEC-LINKED"})
 assert report["fallback_trace"]==["19","48"]
 assert report["fallback_evidence_status"]=="linked_to_scheduler_decision"
 assert "19 → 48" in report["markdown"]
def test_budget_guard_and_external_boundary():
 out=budget(demo_records())
 assert out["maximum_total_attempts"]==2 and out["external_platform_modified"] is False
 assert out["summary"]["total_spend"]=="0.1150"
 assert out["summary"]["remaining_budget"]=="0.0850"
 assert out["summary"]["usage_percentage"]=="57.5"
 assert "0.11499999999999999" not in json.dumps(out)
 assert out["by_channel"][0]["channel_name"]=="玫瑰三号"
 assert out["by_result"] and out["highest_cost_requests"][0]["request_id"]=="D003"
 assert out["anomalies"][0]["type"]=="cost_concentration"
def test_budget_decimal_edge_cases_and_missing_evidence():
 zero=budget([])
 assert zero["summary"]["total_spend"]=="0.0000" and zero["summary"]["usage_percentage"]=="0.0"
 tiny=budget([{"request_id":"tiny","cost_cny":"0.000001","source_type":"measured_uat"}])
 assert tiny["highest_cost_requests"][0]["actual_cost"]=="0.000001"
 above=budget([{"request_id":"over","cost_cny":"0.3000","source_type":"measured_uat"}])
 assert above["summary"]["status"]=="unlimited" and above["summary"]["remaining_budget"] is None
 missing=budget([{"request_id":"missing","cost_cny":"","source_type":"measured_uat"}])
 assert missing["highest_cost_requests"][0]["actual_cost"] is None
 assert any(x["type"]=="missing_cost_evidence" for x in missing["anomalies"])
def test_utf8_sig_chinese_and_mojibake_warning():
 names=["玫瑰-多模型测试","青绿四号","北京云雀未来-Deepseek","阿里云ADB-DeepSeek-Test","Test-Duan-DeepSeek测试","水循环","渠道健康","成本与预算"]
 content=("\ufeffrequest_id,channel_id,requested_model,channel_name\n"+"\n".join(f"r{i},19,m,{name}" for i,name in enumerate(names))).encode("utf-8")
 rows=parse_content("utf8.csv",content)
 assert [r["channel_name"] for r in rows]==names
 assert validate_rows(rows)["rejected_count"]==0
 warned=validate_rows([{"request_id":"m","channel_id":"19","requested_model":"x","channel_name":"Ã¥Â"}])
 assert any(x["code"]=="likely_mojibake" for x in warned["issues"])
 with pytest.raises(ValueError,match="unsupported_text_encoding"): parse_content("bad.csv",b"\xff\xfe")
def test_mapping_drift_and_sample_count():
 out=mappings(demo_records())
 assert any(x["mapping_status"]=="unexpected_model" for x in out)
 assert all(x["observed_count"]>=1 for x in out)
def test_mapping_evidence_source_reflects_real_rows_not_a_hardcoded_label():
 demo_out=mappings(demo_records())
 assert all(x["evidence_source"]=="demo_mock" for x in demo_out)
 uat_row={"channel_id":"48","requested_model":"deepseek-v4-flash","actual_model":"deepseek-v4-flash","source_type":"measured_uat"}
 uat_out=mappings([uat_row])
 assert uat_out[0]["evidence_source"]=="measured_uat"
def test_compatibility_states_and_http_200_limitation():
 out=compatibility()
 assert len(out)==12 and {x["status"] for x in out}<={"supported","unsupported","passed","failed","pending_confirmation","not_tested"}
 assert all("HTTP 200" in x["notes"] for x in out)
def test_config_review_blocks_and_never_applies():
 out=config_review({"weights":{"a":-1,"b":1},"fallback":None})
 assert out["status"]=="block" and out["external_configuration_changed"] is False
 assert {"negative_weights","timeout_missing"}<={x["rule"] for x in out["issues"]}
def test_store_is_auditable(tmp_path):
 store=Store(tmp_path/"app.db"); batch=import_preview("x.csv",b"request_id,channel_id,requested_model\nr,19,m\n")
 store.save_batch(batch)
 assert store.batches()[0]["batch_id"]==batch["batch_id"]
