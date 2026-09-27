"""Offline task -> model -> channel pipeline with append-only demonstration logs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from model_capability_router import route_model
from model_channel_resolver import resolve_model_channels
from task_classifier import build_task_profile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "output" / "model_selection_decisions_v1.jsonl"


def select_for_request(request_id: str, text: str) -> dict[str, Any]:
    profile = build_task_profile(text)
    model_result = route_model(profile)
    channels = resolve_model_channels(model_result["selected_model"])
    final_channel = channels[0]["channel_id"] if channels else None
    canonical = json.dumps(
        {"request_id": request_id, "text": text},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return {
        "decision_id": "MODEL-" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()[:16].upper(),
        "request_id": request_id,
        "user_request": text,
        "detected_task": profile,
        "model_ranking": model_result["ranking"],
        "selected_model": model_result["selected_model"],
        "model_selection_reason": model_result["reason"],
        "channel_candidates": channels,
        "final_channel_decision": final_channel,
        "source_type": "offline_demonstration_only",
        "real_api_called": False,
    }


def write_log(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8", newline="\n",
    )


def build_demo() -> list[dict[str, Any]]:
    return [
        select_for_request(
            "MODEL-DEMO-001",
            "Explain transformer architecture and analyze its reasoning workflow.",
        ),
        select_for_request(
            "MODEL-DEMO-002",
            "Write Python code to calculate a matrix equation and debug the function.",
        ),
        select_for_request(
            "MODEL-DEMO-003",
            "Summarize this long document with the included images.",
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rows = build_demo()
    if not args.dry_run:
        write_log(args.output, rows)
    print(json.dumps({
        "status": "ok", "decision_count": len(rows),
        "dry_run": args.dry_run, "real_api_calls_performed": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
