import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from channel_adapter import MockChannelAdapter  # noqa: E402
from decision_logger import DecisionLogger  # noqa: E402
from scheduler import Scheduler  # noqa: E402

BASE = {"request_id": "R1", "requested_model": "deepseek-v4-flash", "stream": False, "input_tokens": 1000, "output_tokens": 500, "currency": "CNY", "strategy": "confidence_aware_v2"}


def make(tmp_path, **kwargs):
    return Scheduler(logger=DecisionLogger(tmp_path / "runtime.jsonl"), id_generator=lambda req, index: f"FIXED-{index}", **kwargs)


def test_four_modes(tmp_path):
    scheduler = make(tmp_path)
    results = {mode: scheduler.route({**BASE, "request_id": mode, "mode": mode}) for mode in ("simulation", "mock_execute", "real_shadow", "real_execute")}
    assert results["simulation"]["execution_attempted"] is False
    assert results["mock_execute"]["execution_attempted"] is True
    assert results["real_shadow"]["execution_attempted"] is False
    assert results["real_execute"]["status"] == "blocked"
    assert all(not row["network_called"] for row in results.values())


def test_mock_execute_calls_adapter_once(tmp_path):
    class Counting(MockChannelAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def send(self, *args, **kwargs):
            self.calls += 1
            return super().send(*args, **kwargs)

    adapter = Counting()
    result = make(tmp_path, adapter=adapter).route({**BASE, "mode": "mock_execute"})
    assert adapter.calls == 1 and result["execution_status"] == "success"


def test_real_shadow_and_real_execute_never_call_adapter(tmp_path):
    class Exploding(MockChannelAdapter):
        def send(self, *args, **kwargs):
            raise AssertionError("must not execute")

    scheduler = make(tmp_path, adapter=Exploding())
    assert scheduler.route({**BASE, "mode": "real_shadow"})["recommended_candidate"] == "REAL-CHANNEL-19"
    assert scheduler.route({**BASE, "request_id": "R2", "mode": "real_execute"})["error_category"] == "real_execution_not_authorized"


def test_strategy_engine_is_called(monkeypatch, tmp_path):
    import scheduler as module
    original = module.strategy_engine.strategy_decision
    calls = []

    def wrapper(**kwargs):
        calls.append(kwargs["strategy_name"])
        return original(**kwargs)

    monkeypatch.setattr(module.strategy_engine, "strategy_decision", wrapper)
    make(tmp_path).route({**BASE, "mode": "simulation"})
    assert calls == ["confidence_aware_v2"]


def test_runtime_log_ids_queries_and_no_fallback_execution(tmp_path):
    scheduler = make(tmp_path)
    first = scheduler.route({**BASE, "mode": "simulation"})
    second = scheduler.route({**BASE, "request_id": "R2", "mode": "simulation"})
    assert first["decision_id"] != second["decision_id"] and first["fallback_executed"] is False
    assert scheduler.logger.find_by_decision_id("FIXED-0")["request_id"] == "R1"
    assert scheduler.logger.find_by_request_id("R2")[0]["decision_id"] == "FIXED-1"


def test_mock_execute_falls_back_to_second_candidate_on_retryable_failure(tmp_path):
    scheduler = make(tmp_path)
    result = scheduler.route({**BASE, "mode": "mock_execute"})
    primary = result["recommended_candidate"]
    backup = result["fallback_order"][0]

    class FailPrimaryThenSucceed(MockChannelAdapter):
        def send(self, request, candidate):
            if candidate["candidate_id"] == primary:
                return {**super().send(request, candidate), "status": "timeout", "error_category": "upstream_timeout"}
            return super().send(request, candidate)

    scheduler = make(tmp_path, adapter=FailPrimaryThenSucceed())
    outcome = scheduler.route({**BASE, "request_id": "R-FALLBACK", "mode": "mock_execute"})
    assert outcome["fallback_executed"] is True
    assert outcome["fallback_trace"] == [primary, backup]
    assert outcome["execution_status"] == "success"
    assert outcome["error_category"] is None
    assert outcome["stopped_reason"] == "success"
    assert len(outcome["attempts"]) == 2
    assert outcome["attempts"][0] == {
        "attempt_number": 1, "candidate_id": primary, "channel_id": outcome["attempts"][0]["channel_id"],
        "result": "failed", "error_category": "upstream_timeout", "is_mock": True,
        "network_called": False, "latency_ms": outcome["attempts"][0]["latency_ms"], "adapter_status": "timeout",
    }
    assert outcome["attempts"][1]["candidate_id"] == backup and outcome["attempts"][1]["result"] == "success"
    assert all(a["network_called"] is False for a in outcome["attempts"])
    assert outcome["selected_target_id"] == primary
    assert outcome["executed_candidate_id"] == backup
    assert outcome["executed_channel_id"] == outcome["attempts"][-1]["channel_id"]
    assert outcome["authoritative_actual_channel"] is None
    assert outcome["attribution_status"] == "mock_execution_not_authoritative"


def test_scheduler_persists_intent_without_inventing_actual_channel(tmp_path):
    from backend.scheduler_attribution_service import SchedulerAttributionService

    store = SchedulerAttributionService(tmp_path / "attribution.sqlite3")
    scheduler = make(tmp_path, attribution_store=store)
    result = scheduler.route({**BASE, "mode": "real_shadow", "metadata": {
        "run_id": "RUN-ATTR-1", "environment_id": "china_uat",
        "request_profile_id": "P01",
    }})
    chain = store.chain(result["decision_id"])
    assert chain["status"] == "authoritative_execution_attribution_missing"
    assert chain["scheduler_decision"]["selected_target_id"] == result["recommended_candidate"]
    assert chain["authoritative_actual_channel"] is None


def test_mock_execute_stops_immediately_on_non_fallback_allowed_error(tmp_path):
    class AlwaysAuthFails(MockChannelAdapter):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def send(self, request, candidate):
            self.calls += 1
            return {**super().send(request, candidate), "status": "authentication_failed", "error_category": "user_authentication_error"}

    adapter = AlwaysAuthFails()
    scheduler = make(tmp_path, adapter=adapter)
    result = scheduler.route({**BASE, "mode": "mock_execute"})
    assert adapter.calls == 1
    assert len(result["attempts"]) == 1
    assert result["fallback_executed"] is False
    assert result["error_category"] == "user_authentication_error"
    assert result["stopped_reason"] == "non_retryable_error_or_attempt_limit"


def test_mock_execute_bounded_by_configured_max_attempts(tmp_path):
    class AlwaysTimesOut(MockChannelAdapter):
        def send(self, request, candidate):
            return {**super().send(request, candidate), "status": "timeout", "error_category": "upstream_timeout"}

    scheduler = make(tmp_path, adapter=AlwaysTimesOut())
    result = scheduler.route({**BASE, "mode": "mock_execute"})
    assert len(result["attempts"]) == scheduler.fallback_policy.max_attempts == 2
    assert result["execution_status"] == "timeout"
    assert result["stopped_reason"] == "non_retryable_error_or_attempt_limit"
    assert all(a["network_called"] is False for a in result["attempts"])


def test_fallback_policy_is_derived_from_the_canonical_taxonomy(tmp_path):
    scheduler = make(tmp_path)
    taxonomy_categories = set(scheduler.error_taxonomy["categories"])
    assert set(scheduler.fallback_policy.retryable_errors) | set(scheduler.fallback_policy.non_retryable_errors) == taxonomy_categories
    assert not (set(scheduler.fallback_policy.retryable_errors) & set(scheduler.fallback_policy.non_retryable_errors))


def test_protected_hashes_unchanged(tmp_path):
    paths = [ROOT / "data" / "real_channel_measurements_v1.csv", ROOT / "data" / "channel_catalog_real_v1.csv", ROOT / "data" / "mock_channel_catalog_v3.csv", ROOT / "src" / "strategy_engine.py", ROOT / "config" / "strategy_catalog_v2.json"]
    before = {path: hashlib.sha256(path.read_bytes()).digest() for path in paths}
    make(tmp_path).route({**BASE, "mode": "real_shadow"})
    assert before == {path: hashlib.sha256(path.read_bytes()).digest() for path in paths}


def test_runtime_sources_have_no_network_imports():
    for name in ("candidate_resolver.py", "execution_guard.py", "channel_adapter.py", "scheduler.py", "run_scheduler.py"):
        source = (ROOT / "src" / name).read_text(encoding="utf-8")
        assert not any(token in source for token in ("import requests", "import httpx", "import urllib", "import socket", "aiohttp"))


class FakePriceCatalog:
    def __init__(self, *, status="fresh"):
        self.status = status
        self.calls = []

    def resolve(self, **identity):
        self.calls.append(identity)
        if self.status != "fresh":
            return {
                **identity, "status": self.status, "eligible": False,
                "reason": f"price_{self.status}", "network_called": False,
            }
        return {
            **identity, "status": "fresh", "eligible": True,
            "input_unit_price": "0.125", "output_unit_price": "0.5",
            "billing_unit": "per_1m_tokens",
            "catalog_version": "PRICE-CATALOG-TEST-V1",
            "record_sha256": "a" * 64,
            "network_called": False,
        }


def test_versioned_price_catalog_is_bound_to_decision_and_candidate_ranking(tmp_path):
    prices = FakePriceCatalog()
    result = make(tmp_path, price_catalog_service=prices).route({
        **BASE, "mode": "simulation",
        "metadata": {"environment_id": "china_uat"},
    })
    assert result["price_context"] == {
        "provider": "versioned_price_catalog",
        "status": "fresh",
        "catalog_versions": ["PRICE-CATALOG-TEST-V1"],
        "resolved_count": result["candidate_count"],
        "blocked_count": 0,
        "network_called": False,
    }
    assert prices.calls
    assert all(call["environment_id"] == "china_uat" for call in prices.calls)
    assert all(row["price_catalog_version"] == "PRICE-CATALOG-TEST-V1"
               for row in result["candidate_ranking"])
    assert all(row["price_freshness"] == "fresh"
               for row in result["candidate_ranking"])


def test_stale_price_catalog_blocks_routing_without_embedded_price_fallback(tmp_path):
    result = make(
        tmp_path, price_catalog_service=FakePriceCatalog(status="stale")
    ).route({
        **BASE, "mode": "simulation",
        "metadata": {"environment_id": "china_uat"},
    })
    assert result["recommended_candidate"] is None
    assert result["eligible_count"] == 0
    assert result["price_context"]["status"] == "blocked"
    assert result["price_context"]["blocked_count"] == result["candidate_count"]
    assert {row["routing_block_reason"] for row in result["candidate_ranking"]} == {
        "price_stale"
    }


def test_configured_price_catalog_requires_explicit_environment_scope(tmp_path):
    prices = FakePriceCatalog()
    result = make(tmp_path, price_catalog_service=prices).route({
        **BASE, "mode": "simulation",
    })
    assert not prices.calls
    assert result["recommended_candidate"] is None
    assert result["price_context"]["status"] == "blocked"
    assert {row["routing_block_reason"] for row in result["candidate_ranking"]} == {
        "price_environment_scope_required"
    }
