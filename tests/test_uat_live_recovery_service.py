from backend.uat_live_recovery_service import UatLiveRecoveryService


class Logs:
    def __init__(self): self.attempts=[]; self.saved=None
    def start_execution(self, **kwargs): self.started=kwargs; return "CALL-1"
    def record_attempt(self, **kwargs): self.attempts.append(kwargs)
    def finish_execution(self, _record, **kwargs): self.finished=kwargs
    def save_routing_decision(self, **kwargs): self.saved=kwargs


class Circuit:
    def before_request(self, circuit, request): return {"allowed":True,"state":"CLOSED","probe_lease_id":None}
    def record_success(self, *args): return {"state":"CLOSED"}
    def record_failure(self, *args): return {"state":"CLOSED"}


def service():
    logs=Logs(); return UatLiveRecoveryService(call_logs=logs,circuit_breakers=Circuit()),logs


def test_429_falls_back_once_and_preserves_single_decision():
    item,logs=service()
    result=item.execute(environment_id="china_uat",acceptance_run_id="AR-1",
      models=["primary","backup"],stream=False,endpoint_type="uat_http_post",
      strategy_variant="acceptance",executor=lambda model,_n: (
        {"execution_status":"failed","http_status":429,"error_category":"rate_limited"}
        if model=="primary" else {"execution_status":"success","http_status":200,
          "actual_model":"backup","output_tokens":3}))
    assert result["fallback_used"] is True and result["selected_model"]=="backup"
    assert len(result["attempts"])==2 and len(logs.attempts)==1
    assert logs.finished["total_attempts"]==2 and logs.saved["result"]["decision_id"]==result["decision_id"]


def test_output_started_forbids_fallback():
    item,_=service()
    result=item.execute(environment_id="china_uat",acceptance_run_id="AR-1",
      models=["primary","backup"],stream=True,endpoint_type="uat_http_post",
      strategy_variant="acceptance",executor=lambda *_: {"execution_status":"failed",
        "http_status":502,"error_category":"sse_protocol_error","output_started":True})
    assert len(result["attempts"])==1
    assert result["stopped_reason"]=="output_started_fallback_forbidden"


def test_400_does_not_retry():
    item,_=service()
    result=item.execute(environment_id="china_uat",acceptance_run_id="AR-1",
      models=["primary","backup"],stream=False,endpoint_type="uat_http_post",
      strategy_variant="acceptance",executor=lambda *_: {"execution_status":"failed",
        "http_status":400,"error_category":"user_parameter_error"})
    assert len(result["attempts"])==1 and result["stopped_reason"]=="non_retryable_error"
