from backend.call_log_service import CallLogService


def test_routing_decision_persists_candidates_and_execution(tmp_path):
    logs=CallLogService(tmp_path/"decisions.db")
    record=logs.start_execution(request_id="REQ-1",decision_id="DEC-1",environment_id="china_uat",requested_model="model-a",stream=False)
    logs.finish_execution(record,status="SUCCESS",response_id="RESP-1",actual_model="model-a",http_status=200,total_latency_ms=125,input_tokens=10,output_tokens=20,total_attempts=1)
    result={"decision_id":"DEC-1","request_id":"REQ-1","response_id":"RESP-1","requested_model":"model-a","actual_model":"model-a","method":"POST","path":"/v1/chat/completions"}
    routing={"policy":"latency_first","selected_model":"model-a","selection_reason":"recent_live_success_evidence","confidence":"observed","catalog_candidate_count":2,"candidates":[{"model_id":"model-a","score":9,"reason":"recent_live_success_evidence"},{"model_id":"model-b","score":1,"reason":"real_catalog_pending_validation"}],"excluded":[]}
    logs.save_routing_decision(result=result,routing_decision=routing)
    detail=logs.routing_decision("DEC-1")
    assert detail and detail["request_id"]=="REQ-1"
    assert detail["decision_type"]=="automatic_routing"
    assert len(detail["candidates"])==2
    assert detail["execution_result"]["input_tokens"]==10
    assert logs.list_routing_decisions(request_id="REQ-1")["total"]==1


def test_existing_real_log_reconstructs_minimum_specified_decision(tmp_path):
    logs=CallLogService(tmp_path/"legacy.db")
    record=logs.start_execution(request_id="REQ-OLD",decision_id="DEC-OLD",environment_id="china_uat",requested_model="kimi-k3",stream=False)
    logs.finish_execution(record,status="SUCCESS",actual_model="kimi-k3",http_status=200,total_latency_ms=16086,input_tokens=88,output_tokens=384,total_attempts=1)
    detail=logs.routing_decision("DEC-OLD")
    assert detail and detail["decision_type"]=="specified_model"
    assert detail["catalog_candidate_count"]==1
    assert detail["selected_model"]=="kimi-k3"
    assert detail["selected_channel"] is None
