"""Single source of truth for the 17-category runtime error taxonomy.

Both the Console API (src/services/console_service.py) and the Scheduler
Runtime fallback policy (src/scheduler.py) derive their classification and
retry/fallback eligibility from this file, so the two layers cannot drift
apart. This is a separate, versioned taxonomy from the Week 2 measurement
layer (data/error_taxonomy.csv), which stays unchanged as protected
historical evidence tied to imported request records. See
docs/ERROR_TAXONOMY_UNIFICATION.md for the crosswalk between the two.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import re

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAXONOMY_PATH = ROOT / "data" / "error_taxonomy_runtime_v1.json"

REQUIRED_CATEGORY_FIELDS = {
    "error_code", "chinese_name", "error_layer", "typical_http_status",
    "retryable", "fallback_allowed", "recommended_action", "detection_rule",
    "measurement_taxonomy_mapping",
}


class ErrorTaxonomyError(ValueError):
    pass


_SENSITIVE_ERROR = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?\S+|bearer\s+[A-Za-z0-9._~+/-]+|"
    r"sk-[A-Za-z0-9_-]{12,}|api[_-]?key\s*[:=]\s*\S+|cookie\s*[:=]\s*\S+)"
)


def redact_error_message(value: Any, *, maximum_length: int = 240) -> str:
    """Return a bounded diagnostic that cannot echo common credential forms."""
    safe = _SENSITIVE_ERROR.sub("[REDACTED]", str(value or ""))
    safe = " ".join(safe.split())
    return safe[:maximum_length]


def classify_runtime_error(
    *, status: int | None = None, message: str = "", context: str = "",
    taxonomy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify execution failures through the canonical versioned taxonomy.

    Rule order is intentional and shared by the Console and execution engine.
    The caller supplies only safe status/message/context observations; raw
    response bodies and credentials are never returned.
    """
    loaded = taxonomy or load_taxonomy()
    normalized_message = str(message or "").casefold()
    normalized_context = str(context or "").casefold()
    if "json" in normalized_message:
        category = "local_request_error"
    elif status == 400:
        category = "user_parameter_error"
    elif status in (401, 403) and normalized_context == "channel":
        category = "channel_authentication_error"
    elif status in (401, 403):
        category = "user_authentication_error"
    elif status in (404, 503) and "model" in normalized_message:
        category = "model_not_supported"
    elif status == 429:
        category = "rate_limited"
    elif status == 504:
        category = "gateway_timeout"
    elif status is not None and status >= 500:
        category = "upstream_5xx"
    elif "timeout" in normalized_message:
        category = "upstream_timeout"
    elif "sse" in normalized_message:
        category = "sse_incomplete"
    elif "schema" in normalized_message:
        category = "response_schema_error"
    elif "protocol" in normalized_message or normalized_context == "protocol":
        category = "protocol_error"
    elif ("connection" in normalized_message or "connect" in normalized_message
          or normalized_context == "network"):
        category = "network_transport_error"
    elif "budget" in normalized_message:
        category = "budget_exceeded"
    elif "not_authorized" in normalized_message or normalized_context == "guard":
        category = "execution_not_authorized"
    elif "no_candidate" in normalized_message or normalized_context == "routing":
        category = "all_candidates_unavailable"
    else:
        category = "unknown_error"
    metadata = loaded["categories"][category]
    return {
        "error_category": category,
        "error_code": metadata["error_code"],
        "error_layer": metadata["error_layer"],
        "retryable": bool(metadata["retryable"]),
        "fallback_allowed": bool(metadata["fallback_allowed"]),
        "recommended_action": metadata["recommended_action"],
        "sanitized_message": redact_error_message(message),
        "original_status": status,
        "classification_rule_version": loaded["taxonomy_version"],
    }


def load_taxonomy(path: str | Path = DEFAULT_TAXONOMY_PATH) -> dict[str, Any]:
    """Load and validate the canonical runtime taxonomy.

    Returns a dict with ``taxonomy_version`` and ``categories`` (a mapping of
    category name to its metadata). Raises ``ErrorTaxonomyError`` if the file
    is missing required fields or categories are malformed.
    """
    with Path(path).open(encoding="utf-8-sig") as handle:
        data = json.load(handle)
    if "taxonomy_version" not in data or "categories" not in data:
        raise ErrorTaxonomyError("taxonomy file missing taxonomy_version or categories")
    categories = data["categories"]
    if not categories:
        raise ErrorTaxonomyError("taxonomy must define at least one category")
    for name, meta in categories.items():
        missing = REQUIRED_CATEGORY_FIELDS - set(meta)
        if missing:
            raise ErrorTaxonomyError(f"category {name} missing fields: {', '.join(sorted(missing))}")
        if not isinstance(meta["fallback_allowed"], bool):
            raise ErrorTaxonomyError(f"category {name} fallback_allowed must be a boolean")
    return {"taxonomy_version": str(data["taxonomy_version"]), "categories": categories}


def fallback_allowed_categories(taxonomy: dict[str, Any]) -> list[str]:
    return sorted(name for name, meta in taxonomy["categories"].items() if meta["fallback_allowed"])


def fallback_blocked_categories(taxonomy: dict[str, Any]) -> list[str]:
    return sorted(name for name, meta in taxonomy["categories"].items() if not meta["fallback_allowed"])
