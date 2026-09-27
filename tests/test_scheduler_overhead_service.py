from backend.scheduler_overhead_service import SchedulerOverheadService


def test_operator_projection_explains_scope_and_exclusions(tmp_path):
    service = SchedulerOverheadService(tmp_path / "timing.sqlite3")
    service.recorder.record(
        decision_id="D1", run_id="R1", stage="scheduler_total", duration_ms=2.5
    )
    result = service.status(window_minutes=60)
    assert result["status"] == "ready"
    assert result["metric_definition"] == "scheduler_compute_and_local_persistence_only"
    assert "provider_network_latency" in result["excluded_time"]
    assert result["network_called"] is False
    assert result["performance_budget"]["status"] == "insufficient_samples"
    assert len(result["performance_budget"]["policy_sha256"]) == 64


def test_versioned_performance_budget_passes_and_fails_with_enough_samples(tmp_path):
    service = SchedulerOverheadService(tmp_path / "timing.sqlite3")
    for index in range(30):
        for stage in ("candidate_loading", "metric_lookup",
                      "eligibility_and_scoring", "decision_persistence",
                      "audit_emission", "scheduler_total"):
            service.recorder.record(
                decision_id=f"D{index}", run_id="R", stage=stage,
                duration_ms=1.0)
    assert service.status()["performance_budget"]["status"] == "pass"
    failing = SchedulerOverheadService(tmp_path / "slow.sqlite3")
    for index in range(30):
        for stage in ("candidate_loading", "metric_lookup",
                      "eligibility_and_scoring", "decision_persistence",
                      "audit_emission", "scheduler_total"):
            failing.recorder.record(
                decision_id=f"S{index}", run_id="R", stage=stage,
                duration_ms=100.0)
    assert failing.status()["performance_budget"]["status"] == "fail"


def test_real_performance_dashboard_separates_scheduler_and_provider(tmp_path):
    service = SchedulerOverheadService(tmp_path / "performance.sqlite3")
    service.recorder.record_trace(
        local_request_id="REQ-1", provider_request_id="provider-1", decision_id="DEC-1",
        environment_id="china_uat", tenant_id="tenant-1", strategy_id="latency_first",
        model_id="kimi-k2.6", traffic_class="business", stream=False,
        stage_durations={"request_validation":1.0,"candidate_discovery":2.0,
                         "scoring":3.0,"scheduler_total":6.0},
        provider_latency_ms=120.0,end_to_end_ms=129.0,success=True,
        candidate_count=5,filtered_count=2)
    result=service.performance(environment_id="china_uat",window_minutes=1440)
    assert result["kpis"]["request_count"] == 1
    assert result["kpis"]["scheduler_p95_ms"] == 6.0
    assert result["provider_comparison"][0]["provider_ms"] == 120.0
    assert result["provider_comparison"][0]["end_to_end_ms"] == 129.0
    assert result["strategy_comparison"][0]["average_candidate_count"] == 5.0


def test_probe_traffic_is_excluded_from_business_performance(tmp_path):
    service = SchedulerOverheadService(tmp_path / "performance.sqlite3")
    for traffic,request_id in (("business","REQ-B"),("probe","REQ-P")):
        service.recorder.record_trace(local_request_id=request_id,decision_id=None,
          environment_id="china_uat",tenant_id="tenant-1",strategy_id="specified_model",
          model_id="glm-5.2",traffic_class=traffic,stream=True,
          stage_durations={"request_validation":1.0,"scheduler_total":1.0},
          provider_latency_ms=10.0,end_to_end_ms=12.0,success=True)
    business=service.performance(environment_id="china_uat",traffic_class="business")
    probe=service.performance(environment_id="china_uat",traffic_class="probe")
    assert business["kpis"]["request_count"] == 1
    assert probe["kpis"]["request_count"] == 1
    assert business["recent_records"][0]["local_request_id"] == "REQ-B"
