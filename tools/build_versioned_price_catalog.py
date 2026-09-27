"""Build the immutable China-UAT model price catalog from standardized logs.

The script does not access a network and never infers prices from model names.
Only repeated, internally consistent per-token evidence is confirmed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.price_catalog_service import PriceCatalogService
PRICE_RE = re.compile(
    r"输入\s+¥(?P<input>\d+(?:\.\d+)?)\s*/\s*1M tokens;\s*"
    r"输出\s+¥(?P<output>\d+(?:\.\d+)?)\s*/\s*1M tokens;\s*"
    r"缓存输入\s+¥(?P<cached>\d+(?:\.\d+)?)\s*/\s*1M tokens"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(db_path: Path, version: str) -> dict:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT record_id,actual_model,requested_model,billed_model,
          provider,pricing_detail,occurred_at FROM standardized_call_logs
          WHERE source_type='historical_uat_csv' AND duplicate_of IS NULL
          ORDER BY occurred_at,record_id""").fetchall()
    by_model: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_model[row["actual_model"]].append(row)
    records = []
    evidence = []
    for model, items in sorted(by_model.items()):
        parsed = []
        for row in items:
            match = PRICE_RE.search(row["pricing_detail"] or "")
            if match:
                parsed.append((match.group("input"), match.group("cached"),
                               match.group("output")))
        distinct = sorted(set(parsed))
        confirmed = len(distinct) == 1 and len(parsed) == len(items)
        price = distinct[0] if confirmed else (None, None, None)
        record_ids = [row["record_id"] for row in items]
        reference = hashlib.sha256("\n".join(record_ids).encode()).hexdigest()
        records.append({
            "model_id": model,
            "requested_model": items[0]["requested_model"],
            "billed_model": items[0]["billed_model"],
            "provider": items[0]["provider"], "currency": "CNY",
            "input_price_per_million_tokens": price[0],
            "cached_input_price_per_million_tokens": price[1],
            "output_price_per_million_tokens": price[2],
            "per_request_price": None, "pricing_unit": "per_1m_tokens",
            "confidence": "observed_repeated" if confirmed else "insufficient_evidence",
            "verification_status": "confirmed" if confirmed else "pending_confirmation",
        })
        evidence.append({"model_id": model, "record_count": len(items),
            "parsed_count": len(parsed), "distinct_price_count": len(distinct),
            "record_set_sha256": reference,
            "verification_status": records[-1]["verification_status"]})
    source_reference = "standardized_call_logs:" + hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    service = PriceCatalogService(db_path, ROOT / "config" / "price_sync_policy_v1.json",
                                  development_mode=True)
    result = service.import_model_price_version(
        price_version=version, environment_id="china_uat",
        effective_from="2026-08-06T00:00:00Z", effective_to=None,
        source_type="historical_log_detail", source_reference=source_reference,
        records=records)
    payload = {"result": result, "source_row_count": len(rows),
        "evidence": evidence, "database_sha256": sha256(db_path)}
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path,
        default=ROOT / "data" / "routing_quality_console.sqlite3")
    parser.add_argument("--version", default="price-catalog-china-uat-20260806-v1")
    parser.add_argument("--output", type=Path,
        default=ROOT / "evidence" / "pricing" / "price-catalog-china-uat-20260806-v1.json")
    args = parser.parse_args()
    result = build(args.database, args.version)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["result"], ensure_ascii=True))
