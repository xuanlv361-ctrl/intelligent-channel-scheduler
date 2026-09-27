from src.services.console_service import compatibility


def test_http_200_does_not_create_compatibility_pass():
    result = compatibility([
        {"request_id": "r", "http_status": 200, "source_type": "measured_uat"}
    ])
    assert all(item["status"] == "not_tested" for item in result)
    assert all(item["evidence_count"] == 0 for item in result)


def test_only_explicit_compatibility_evidence_changes_status():
    result = compatibility([
        {"request_id": "r1", "timestamp": "2026-07-28T01:00:00Z",
         "source_type": "measured_uat",
         "compatibility_results": {"streaming_sse": "passed"}},
        {"request_id": "r2", "timestamp": "2026-07-28T02:00:00Z",
         "source_type": "measured_uat",
         "compatibility_results": {"structured_output": "failed"}},
    ])
    by_name = {item["capability"]: item for item in result}
    assert by_name["streaming_sse"]["status"] == "passed"
    assert by_name["streaming_sse"]["evidence_request_ids"] == ["r1"]
    assert by_name["structured_output"]["status"] == "failed"
    assert by_name["non_streaming_text"]["status"] == "not_tested"
