"""Run an offline historical replay against a selected local runtime database.

The output contains only safe identifiers and aggregate evidence.  The script
does not contain a transport and cannot call UAT or a model provider.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.historical_replay_service import HistoricalReplayService


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate", default="latency_first",
                        choices=("latency_first", "cost_first"))
    args = parser.parse_args()
    service = HistoricalReplayService(Path(args.database))
    result = service.run(baseline_strategy="actual_observed",
                         candidate_strategy=args.candidate,
                         filters={"environment_id": "china_uat"}, limit=1000)
    persisted = service.get(result["replay_id"])
    first_persisted = persisted["items"][0] if persisted["items"] else None
    safe = {
        "replay_id": result["replay_id"], "created_at": result["created_at"],
        "environment_id": result["source"]["environment_id"],
        "occurred_from": result["source"]["occurred_from"],
        "occurred_to": result["source"]["occurred_to"],
        "sample_count": result["sample_count"],
        "exact_association_count": result["exact_association_count"],
        "unlinked_count": result["unlinked_count"],
        "baseline_strategy": result["baseline_strategy"],
        "candidate_strategy": result["candidate_strategy"],
        "choice_changed_count": result["choice_changed_count"],
        "choice_comparable_count": result["choice_comparable_count"],
        "cost": result["cost"], "latency": result["latency"],
        "sla": result["sla"], "risk_count": result["risk_count"],
        "low_confidence_count": result["low_confidence_count"],
        "unestimable_count": result["unestimable_count"],
        "request_ids": [item["request_id"] for item in result["items"]
                        if item["request_id"]][:10],
        "conclusion": result["conclusion"], "network_calls": 0,
        "persistence_verified": {
            "sample_count_stable": persisted["sample_count"] == result["sample_count"],
            "item_count": len(persisted["items"]),
            "source_type_preserved": bool(first_persisted and first_persisted["source_type"]),
            "association_preserved": bool(first_persisted and first_persisted["association"]),
            "timeline_steps": len(first_persisted["timeline"]) if first_persisted else 0,
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(safe, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
