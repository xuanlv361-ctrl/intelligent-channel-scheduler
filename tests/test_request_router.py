import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from request_router import (  # noqa: E402
    FIRST_CLASS_STRATEGIES, RequestRouter, RequestValidationError, RuntimeRequest,
)
import strategy_engine  # noqa: E402


BASE = {"request_id": "R1", "requested_model": "deepseek-v4-flash", "stream": False, "input_tokens": 1, "output_tokens": 2, "currency": "CNY", "strategy": "confidence_aware_v2", "mode": "simulation"}


@pytest.fixture
def router():
    return RequestRouter(strategy_engine.load_json(strategy_engine.CATALOG_PATH)["supported_strategies"])


def test_runtime_request_normalizes(router):
    result = router.route_runtime(BASE)
    assert isinstance(result, RuntimeRequest) and result.stream_required is False


@pytest.mark.parametrize("field,value", [("request_id", ""), ("requested_model", ""), ("stream", "false"), ("input_tokens", -1), ("output_tokens", 1.5), ("currency", "")])
def test_field_validation(router, field, value):
    with pytest.raises(RequestValidationError) as error:
        router.route_runtime({**BASE, field: value})
    assert error.value.field == field


def test_unknown_strategy_and_mode_are_structured(router):
    with pytest.raises(RequestValidationError) as error:
        router.route_runtime({**BASE, "strategy": "nope"})
    assert error.value.error_code == "unsupported_strategy"
    with pytest.raises(RequestValidationError) as error:
        router.route_runtime({**BASE, "mode": "nope"})
    assert error.value.error_code == "unsupported_mode"


@pytest.mark.parametrize("key", [
    "api_key", "Authorization", "client_secret", "access_token", "Cookie",
    "password", "credential", "messages", "prompt", "raw_response",
    "request_body", "storage_state", "content",
])
def test_sensitive_metadata_rejected(router, key):
    with pytest.raises(RequestValidationError) as error:
        router.route_runtime({**BASE, "metadata": {key: "hidden"}})
    assert error.value.error_code == "sensitive_metadata"


def test_legacy_stream_required_alias_is_accepted(router):
    payload = {**BASE}
    payload["stream_required"] = payload.pop("stream")
    assert router.route_runtime(payload).stream is False


@pytest.mark.parametrize("strategy", ["latency_first", "cost_first"])
def test_first_class_strategies_are_accepted_without_alias_rewrite(router, strategy):
    assert strategy in FIRST_CLASS_STRATEGIES
    routed = router.route_runtime({**BASE, "strategy": strategy})
    assert routed.strategy == strategy
    assert routed.to_dict()["strategy"] == strategy


def test_typed_context_token_constraint_is_not_misclassified_as_a_secret(router):
    routed = router.route_runtime({
        **BASE,
        "metadata": {
            "environment_id": "domestic_uat",
            "capability_scenario_id": "long_context",
            "capability_subject_version": "catalog-v1",
            "capability_request_constraints": {
                "context": {"context_tokens": 32768},
            },
        },
        "capability_scope": {"required_modalities": ["text"]},
    })
    assert routed.metadata["capability_request_constraints"]["context"]["context_tokens"] == 32768
