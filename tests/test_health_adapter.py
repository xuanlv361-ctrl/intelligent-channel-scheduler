import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import channel_health  # noqa: E402
import health_adapter  # noqa: E402
import strategy_engine  # noqa: E402
from metrics_store import MetricsStore  # noqa: E402


def snapshot():
    return channel_health.build_health_snapshot(
        MetricsStore.load(),
        channel_health.load_json(channel_health.DEFAULT_POLICY_PATH),
        channel_health.load_json(channel_health.DEFAULT_THRESHOLDS_PATH),
    )


def test_adapter_preserves_candidate_fields_and_appends_health():
    candidate = {"candidate_id": "X", "channel_id": "M001", "latency_ms": "600", "cost": 0.003}
    original = copy.deepcopy(candidate)
    output = health_adapter.adapt_candidates([candidate], snapshot())[0]
    assert candidate == original
    assert all(output[key] == value for key, value in original.items())
    assert {"health_score", "confidence", "status", "health_version"} <= output.keys()
    assert output["status"] in {"healthy", "warning", "degraded"}


def test_missing_health_is_explicit_unknown():
    output = health_adapter.adapt_candidates(
        [{"channel_id": "not-measured", "latency": 700, "cost": 0.002}],
        snapshot(),
    )[0]
    assert output["status"] == "unknown"
    assert output["health_score"] is None
    assert output["confidence"] is None


def test_strategy_engine_compatibility_and_selection_unchanged():
    request, candidates = strategy_engine.sample_request_and_candidates()
    baseline = strategy_engine.strategy_decision(
        **request, candidates=candidates, strategy_name="fastest_first"
    )
    enriched = health_adapter.adapt_candidates(candidates, snapshot())
    result = strategy_engine.strategy_decision(
        **request, candidates=enriched, strategy_name="fastest_first"
    )
    assert result["selected_candidate"] == baseline["selected_candidate"]
    assert len(result["candidate_details"]) == len(baseline["candidate_details"])


def test_adapter_output_preserves_legacy_selection_and_new_confidence_gate():
    request, candidates = strategy_engine.sample_request_and_candidates()
    enriched = health_adapter.adapt_candidates(candidates, snapshot())
    catalog = strategy_engine.load_json(strategy_engine.CATALOG_PATH)
    for name in catalog["supported_strategies"]:
        result = strategy_engine.strategy_decision(
            **request, candidates=enriched, strategy_name=name
        )
        definition = catalog["strategies"][name]
        if definition["type"] == "weighted_wilson_confidence_score":
            assert result["outcome"] == "unroutable"
            assert {item["exclusion_reason"] for item in result["excluded_candidates"]} == {
                "statistical_confidence_not_ready"
            }
        else:
            assert result["outcome"] == "selected"
