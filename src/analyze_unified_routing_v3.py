"""Descriptive analysis of genuine unified-routing observations."""

from __future__ import annotations
import argparse, csv, json, statistics, sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from record_unified_routing_measurement_v3 import DEFAULT_RESULTS_PATH, read_csv

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"

def _number(rows, field):
    return [float(row[field]) for row in rows if row.get(field) not in ("", None)]

def _group(rows, field):
    groups = defaultdict(list)
    for row in rows: groups[row.get(field, "")].append(row)
    return groups

def _summaries(rows, field):
    output = []
    for key, group in sorted(_group(rows, field).items()):
        latency, cost = _number(group, "total_latency_ms"), _number(group, "cost_cny")
        output.append({field: key, "request_count": len(group),
            "success_count": sum(r["result"] == "success" for r in group),
            "failure_count": sum(r["result"] == "failure" for r in group),
            "mean_latency_ms": statistics.fmean(latency) if latency else "",
            "mean_cost_cny": statistics.fmean(cost) if cost else ""})
    return output

def analyze(rows: list[dict[str, str]]) -> dict[str, Any]:
    if not rows or any(row.get("source_type") != "measured_unified_uat" or row.get("is_mock") != "FALSE" for row in rows):
        raise ValueError("analysis requires genuine measured_unified_uat results")
    channel = lambda r: r.get("actual_channel_id") or r.get("actual_channel_name") or ""
    routed = Counter(channel(row) for row in rows if channel(row))
    total_routed = sum(routed.values())
    shares = {key: count / total_routed for key, count in routed.items()} if total_routed else {}
    profile_channels = {profile: dict(Counter(channel(r) for r in group if channel(r)))
                        for profile, group in _group(rows, "request_profile_id").items()}
    ttft = _number([r for r in rows if r["request_profile_id"] == "P04"], "ttft_ms")
    return {
        "analysis_type": "descriptive_observational_unified_routing",
        "causal_channel_superiority_inference_allowed": False,
        "request_count": len(rows), "routing_counts": dict(routed), "routing_shares": shares,
        "maximum_selected_channel_share": max(shares.values(), default=0),
        "herfindahl_hirschman_index": sum(value * value for value in shares.values()),
        "success_count": sum(r["result"] == "success" for r in rows),
        "failure_count": sum(r["result"] == "failure" for r in rows),
        "streaming_mean_ttft_ms": statistics.fmean(ttft) if ttft else None,
        "incomplete_sse_count": sum(r["request_profile_id"] == "P04" and
            (r.get("sse_complete") != "TRUE" or r.get("done_received") != "TRUE") for r in rows),
        "profile_channel_consistency": {p: {"observed_channels": counts,
            "consistently_same_channel": len(counts) == 1 and bool(counts)} for p, counts in profile_channels.items()},
        "missing_evidence_counts": {
            "channel": sum(not channel(r) for r in rows), "http_status": sum(not r.get("http_status") for r in rows),
            "actual_model": sum(not r.get("actual_model") for r in rows),
            "request_id": sum(not r.get("request_id") for r in rows),
            "ttft_p04": sum(r["request_profile_id"] == "P04" and not r.get("ttft_ms") for r in rows)},
    }

def _write_csv(path, rows):
    fields = list(rows[0]) if rows else ["empty"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n"); writer.writeheader(); writer.writerows(rows)

def generate(results_path=DEFAULT_RESULTS_PATH, output_dir=OUTPUT):
    rows = read_csv(results_path); summary = analyze(rows); output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "unified_routing_summary_v3.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    channel_rows = [{"actual_channel": key, "routing_count": count, "routing_share": summary["routing_shares"][key]}
                    for key, count in sorted(summary["routing_counts"].items())]
    _write_csv(output_dir / "unified_routing_by_channel_v3.csv", channel_rows)
    _write_csv(output_dir / "unified_routing_by_profile_v3.csv", _summaries(rows, "request_profile_id"))
    _write_csv(output_dir / "unified_routing_by_session_v3.csv", _summaries(rows, "session_id"))
    sequence = sorted(rows, key=lambda r: r["plan_id"])
    _write_csv(output_dir / "unified_routing_sequence_v3.csv", [{"plan_id": r["plan_id"], "session_id": r["session_id"],
        "round_id": r["round_id"], "request_profile_id": r["request_profile_id"], "actual_channel": (r.get("actual_channel_id") or r.get("actual_channel_name") or ""),
        "result": r["result"], "total_latency_ms": r["total_latency_ms"]} for r in sequence])
    return summary

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--results-file", type=Path, default=DEFAULT_RESULTS_PATH); parser.add_argument("--output-dir", type=Path, default=OUTPUT); args=parser.parse_args(argv)
    try: print(json.dumps(generate(args.results_file, args.output_dir), ensure_ascii=False, indent=2)); return 0
    except (OSError, ValueError, KeyError) as exc: print(json.dumps({"status":"error","error":str(exc)}), file=sys.stderr); return 1
if __name__ == "__main__": raise SystemExit(main())
