import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from error_taxonomy import (  # noqa: E402
    ErrorTaxonomyError,
    fallback_allowed_categories,
    fallback_blocked_categories,
    load_taxonomy,
    classify_runtime_error,
    redact_error_message,
)

DEFAULT_PATH = ROOT / "data" / "error_taxonomy_runtime_v1.json"


def test_default_taxonomy_has_17_categories_with_required_fields():
    taxonomy = load_taxonomy()
    assert len(taxonomy["categories"]) == 17
    for name, meta in taxonomy["categories"].items():
        assert meta["error_code"] and meta["chinese_name"] and meta["error_layer"]
        assert meta["recommended_action"] and meta["detection_rule"]
        assert isinstance(meta["fallback_allowed"], bool)
        assert isinstance(meta["measurement_taxonomy_mapping"], list)


def test_fallback_partition_is_complete_and_disjoint():
    taxonomy = load_taxonomy()
    allowed = fallback_allowed_categories(taxonomy)
    blocked = fallback_blocked_categories(taxonomy)
    assert set(allowed) & set(blocked) == set()
    assert set(allowed) | set(blocked) == set(taxonomy["categories"])
    assert len(allowed) == 10 and len(blocked) == 7


def test_missing_taxonomy_version_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"categories": {}}), encoding="utf-8")
    with pytest.raises(ErrorTaxonomyError):
        load_taxonomy(bad)


def test_category_missing_required_field_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({
        "taxonomy_version": "test-v1",
        "categories": {"broken": {"error_code": "X"}},
    }), encoding="utf-8")
    with pytest.raises(ErrorTaxonomyError):
        load_taxonomy(bad)


def test_non_boolean_fallback_allowed_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    template = json.loads(DEFAULT_PATH.read_text(encoding="utf-8-sig"))
    first = next(iter(template["categories"]))
    template["categories"][first]["fallback_allowed"] = "TRUE"
    bad.write_text(json.dumps(template), encoding="utf-8")
    with pytest.raises(ErrorTaxonomyError):
        load_taxonomy(bad)


def test_empty_categories_is_rejected(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"taxonomy_version": "test-v1", "categories": {}}), encoding="utf-8")
    with pytest.raises(ErrorTaxonomyError):
        load_taxonomy(bad)


@pytest.mark.parametrize("status,message,context,expected", [
    (400, "bad field", "", "user_parameter_error"),
    (401, "denied", "channel", "channel_authentication_error"),
    (429, "limited", "", "rate_limited"),
    (504, "timeout", "", "gateway_timeout"),
    (None, "connection reset", "network", "network_transport_error"),
    (None, "protocol mismatch", "protocol", "protocol_error"),
])
def test_execution_and_console_share_canonical_classification(status, message, context, expected):
    assert classify_runtime_error(status=status, message=message, context=context)["error_category"] == expected


def test_error_redaction_never_echoes_credentials():
    secret = "sk-" + "example-do-not-store-123456"
    result = redact_error_message(f"Authorization: Bearer {secret} Cookie: sid=abc")
    assert secret not in result and "sid=abc" not in result
