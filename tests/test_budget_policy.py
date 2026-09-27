import json

from src.services.console_service import budget


def test_demo_budget_uses_versioned_decimal_policy():
    result = budget([
        {"request_id": "a", "cost_cny": "0.10", "source_type": "demo_mock"},
        {"request_id": "b", "cost_cny": "0.02", "source_type": "demo_mock"},
    ])
    assert result["policy_version"] == "budget-policy-v1.0.0"
    assert result["formula_version"] == "observed-cost-sum-v1"
    assert result["summary"]["total_spend"] == "0.1200"
    assert result["summary"]["currency"] == "CNY"


def test_china_local_budget_is_unlimited_but_actual_cost_is_counted():
    result = budget([
        {"request_id": "a", "cost_cny": "0.300001", "source_type": "measured_uat",
         "environment_id": "china_uat"}
    ])
    assert result["summary"]["status"] == "unlimited"
    assert result["summary"]["budget_limit"] is None
    assert result["summary"]["total_spend"] == "0.3000"
    assert result["highest_cost_requests"][0]["actual_cost"] == "0.300001"


def test_unknown_overseas_currency_and_missing_cost_remain_null():
    result = budget([
        {"request_id": "o", "cost_cny": None, "source_type": "measured_overseas",
         "environment_id": "overseas"}
    ])
    assert result["summary"]["currency"] is None
    assert result["summary"]["budget_limit"] is None
    assert result["highest_cost_requests"][0]["actual_cost"] is None
    assert result["provenance"]["actual_cost_sample_size"] == 0


def test_empty_overseas_budget_keeps_explicit_environment_and_unknown_currency():
    result = budget([], environment_id="overseas")
    assert result["environment_id"] == "overseas"
    assert result["summary"]["currency"] is None
    assert result["source_type"] == "unknown"
    assert result["sample_size"] == 0


def test_environments_are_grouped_and_never_summed():
    result = budget([
        {"request_id": "c", "cost_cny": "1", "source_type": "measured_uat",
         "environment_id": "china_uat"},
        {"request_id": "o", "cost_cny": "2", "source_type": "measured_overseas",
         "environment_id": "overseas"},
    ])
    assert result["currency_aggregation"] == "blocked_mixed_environment"
    assert {group["environment_id"] for group in result["groups"]} == {
        "china_uat", "overseas"
    }
    assert "total_spend" not in result
    assert "3.0000" not in json.dumps(result)
