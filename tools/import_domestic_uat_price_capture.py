"""Import the authenticated /api/pricing capture as an immutable catalog."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.price_catalog_service import PriceCatalogService


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def main() -> dict[str, object]:
    capture_path = ROOT / "evidence" / "provider-correlation" / \
        "provider-interface-capture.json"
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    payload = capture["interfaces"]["/api/pricing"]["body"]
    rows = payload["data"]
    vendors = {int(row["id"]): row["name"] for row in payload["vendors"]}
    captured_at = datetime.fromisoformat(capture["captured_at"])
    effective_from = captured_at.astimezone(timezone.utc).isoformat()
    provider_version = str(payload["pricing_version"])
    price_version = (
        "price-catalog-china-uat-" +
        captured_at.strftime("%Y%m%d%H%M%S") + "-" +
        provider_version[:12] + "-v1")
    # Provider status evidence establishes 500000 quota/CNY unit and 7.3
    # CNY/USD.  One million tokens therefore contributes a factor of
    # 1_000_000 / 500_000 * 7.3 = 14.6 to model_ratio.
    quota_per_unit = Decimal("500000")
    exchange_rate = Decimal("7.3")
    token_factor = Decimal("1000000") / quota_per_unit * exchange_rate
    records = []
    for raw in rows:
        quota_type = int(raw.get("quota_type") or 0)
        ratio = Decimal(str(raw.get("model_ratio") or "0"))
        completion_ratio = Decimal(str(raw.get("completion_ratio") or "0"))
        cache_ratio = raw.get("cache_ratio")
        per_request = Decimal(str(raw.get("model_price") or "0"))
        billing_mode = str(raw.get("billing_mode") or "")
        input_price = output_price = cached_price = request_price = None
        pricing_unit = "per_1m_tokens"
        status = "confirmed"
        if billing_mode == "tiered_expr":
            pricing_unit = "tiered"
            status = "pending_confirmation"
        elif quota_type == 1:
            pricing_unit = "per_request"
            request_price = decimal_text(per_request * exchange_rate)
        elif ratio > 0:
            input_value = ratio * token_factor
            input_price = decimal_text(input_value)
            output_price = decimal_text(input_value * completion_ratio)
            if cache_ratio is not None:
                cached_price = decimal_text(input_value * Decimal(str(cache_ratio)))
        else:
            status = "pending_confirmation"
        raw_sha = hashlib.sha256(canonical(raw)).hexdigest()
        records.append({
            "model_id": raw["model_name"],
            "requested_model": raw["model_name"],
            "actual_model": None,
            "billed_model": raw["model_name"],
            "provider": vendors.get(int(raw.get("vendor_id") or 0), "unknown"),
            "currency": "CNY",
            "input_price_per_million_tokens": input_price,
            "cached_input_price_per_million_tokens": cached_price,
            "output_price_per_million_tokens": output_price,
            "per_request_price": request_price,
            "pricing_unit": pricing_unit,
            "confidence": "provider_pricing_interface",
            "verification_status": status,
            "raw_record_sha256": raw_sha,
            "provider_price_version": raw.get("pricing_version") or provider_version,
            "supported_endpoints": raw.get("supported_endpoint_types") or [],
        })
    service = PriceCatalogService(
        Path(os.environ.get("ROUTING_CONSOLE_DATABASE_PATH") or
             Path.home() / "AppData/Local/IntelligentChannelScheduler/data/"
             "routing_quality_console.sqlite3"),
        ROOT / "config" / "price_sync_policy_v1.json",
        development_mode=True)
    result = service.import_model_price_version(
        price_version=price_version, environment_id="china_uat",
        effective_from=effective_from, effective_to=None,
        source_type="domestic_uat_pricing_api",
        source_reference=(
            "https://uat.weimeta.cn/api/pricing#" +
            capture["interfaces"]["/api/pricing"]["payload_sha256"]),
        records=records)
    evidence = {
        "price_version": price_version,
        "provider_price_version": provider_version,
        "source_endpoint": "https://uat.weimeta.cn/api/pricing",
        "source_payload_sha256":
            capture["interfaces"]["/api/pricing"]["payload_sha256"],
        "formula_source": {
            "status_endpoint": "https://uat.weimeta.cn/api/status",
            "quota_per_unit": decimal_text(quota_per_unit),
            "usd_exchange_rate": decimal_text(exchange_rate),
            "token_price_formula":
                "model_ratio * 1000000 / quota_per_unit * usd_exchange_rate",
            "per_request_formula": "model_price * usd_exchange_rate",
        },
        "record_count": len(records),
        "confirmed_count": sum(
            row["verification_status"] == "confirmed" for row in records),
        "pending_count": sum(
            row["verification_status"] != "confirmed" for row in records),
        "catalog_checksum": result["checksum"],
        "import_idempotent": result["idempotent"],
    }
    target = ROOT / "evidence" / "pricing" / f"{price_version}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(
        evidence, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return evidence


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, sort_keys=True))
