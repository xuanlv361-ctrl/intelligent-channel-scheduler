"""Build deterministic offline scenario candidates for week-three policy tests.

No network service is contacted.  Rows changed by a scenario are explicitly
labelled as mock scenario design data; they are not new real observations.
"""

from __future__ import annotations

import argparse
import csv
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATES = PROJECT_ROOT / "data" / "candidate_channels_v1.csv"
DEFAULT_SCENARIOS = PROJECT_ROOT / "data" / "scenario_catalog_v1.csv"
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "scenario_candidates_v1.csv"

OUTPUT_COLUMNS = [
    "scenario_id", "strategy", "stream_required", "candidate_id", "channel_id",
    "channel_name", "requested_model", "supports_text", "supports_non_stream",
    "supports_stream", "availability_status", "latency_ms", "input_price_per_1m",
    "output_price_per_1m", "currency", "success_rate", "sample_size",
    "confidence_level", "metrics_updated_at", "priority", "data_source", "is_mock",
    "expected_eligible", "expected_exclusion_reason", "expected_selected",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def mark_scenario_mock(row: dict[str, str]) -> None:
    sources = [part for part in row.get("data_source", "").split("+") if part]
    if "mock_scenario_design" not in sources:
        sources.append("mock_scenario_design")
    row["data_source"] = "+".join(sources)
    row["is_mock"] = "TRUE"


def override(row: dict[str, str], **values: object) -> None:
    for key, value in values.items():
        row[key] = "" if value is None else str(value)
    mark_scenario_mock(row)


def tie_candidate(candidate_id: str, channel_id: int, priority: int) -> dict[str, str]:
    return {
        "candidate_id": candidate_id, "channel_id": str(channel_id),
        "channel_name": candidate_id, "requested_model": "deepseek-v4-flash",
        "supports_text": "TRUE", "supports_non_stream": "TRUE", "supports_stream": "TRUE",
        "availability_status": "available", "latency_ms": "900",
        "input_price_per_1m": "1", "output_price_per_1m": "2", "currency": "CNY",
        "success_rate": "0.99", "sample_size": "100", "confidence_level": "medium",
        "metrics_updated_at": "2026-07-21 10:34:12", "priority": str(priority),
        "data_source": "mock_scenario_design", "is_mock": "TRUE",
    }


def exclusion_reason(row: dict[str, str], scenario: dict[str, str]) -> str:
    if row["requested_model"] != scenario["requested_model"]:
        return "model_mismatch"
    if row["availability_status"] != "available":
        return "availability_unavailable"
    if row["currency"] != scenario["currency"]:
        return "currency_mismatch"
    if scenario["stream_required"].upper() == "TRUE":
        stream = row["supports_stream"].upper()
        if stream == "FALSE":
            return "stream_not_supported"
        if stream != "TRUE":
            return "stream_capability_unknown"
    if not row["latency_ms"]:
        return "missing_latency"
    if not row["input_price_per_1m"] or not row["output_price_per_1m"]:
        return "missing_price"
    updated = datetime.strptime(row["metrics_updated_at"], "%Y-%m-%d %H:%M:%S")
    decision = datetime.strptime(scenario["decision_time"], "%Y-%m-%d %H:%M:%S")
    if decision - updated > timedelta(hours=168):
        return "stale_metrics"
    return ""


def scenario_rows(scenario: dict[str, str], base: list[dict[str, str]]) -> list[dict[str, str]]:
    sid = scenario["scenario_id"]
    if sid == "S011":
        rows = [tie_candidate("TIE-A", 201, 10), tie_candidate("TIE-B", 202, 20)]
    elif sid == "S012":
        rows = [tie_candidate("TIE-001", 101, 10), tie_candidate("TIE-002", 102, 10)]
    else:
        rows = deepcopy(base)
        by_id = {row["candidate_id"]: row for row in rows}
        if sid == "S004":
            for row in rows:
                override(row, supports_stream="FALSE")
        elif sid == "S005":
            override(by_id["MOCK-DS-FAST"], availability_status="unavailable")
        elif sid == "S006":
            override(by_id["MOCK-DS-FAST"], latency_ms=None)
        elif sid == "S007":
            override(by_id["MOCK-DS-CHEAP"], input_price_per_1m=None, output_price_per_1m=None)
        elif sid == "S008":
            override(by_id["MOCK-DS-FAST"], metrics_updated_at="2026-07-10 00:00:00")
        elif sid == "S009":
            override(by_id["REAL-DS-48"], latency_ms="300", sample_size="1", confidence_level="medium")
        elif sid == "S010":
            override(by_id["REAL-DS-48"], latency_ms="300", sample_size="100", confidence_level="low")
        elif sid == "S013":
            override(by_id["MOCK-DS-CHEAP"], currency="USD")
        elif sid == "S014":
            for row in rows:
                override(row, availability_status="unavailable")
        elif sid == "S015":
            override(by_id["MOCK-DS-FAST"], requested_model="other-model")
            override(by_id["MOCK-DS-CHEAP"], requested_model="other-model")

    selected = scenario["expected_selected_candidate"]
    result = []
    for row in rows:
        reason = exclusion_reason(row, scenario)
        output = {key: row.get(key, "") for key in OUTPUT_COLUMNS}
        output.update({
            "scenario_id": sid, "strategy": scenario["strategy"],
            "stream_required": scenario["stream_required"],
            "expected_eligible": "FALSE" if reason else "TRUE",
            "expected_exclusion_reason": reason,
            "expected_selected": "TRUE" if selected and row["candidate_id"] == selected else "FALSE",
        })
        result.append(output)
    return result


def validate(rows: list[dict[str, str]], scenarios: list[dict[str, str]]) -> None:
    ids = {row["scenario_id"] for row in rows}
    catalog_ids = {row["scenario_id"] for row in scenarios}
    keys = [(row["scenario_id"], row["candidate_id"]) for row in rows]
    selected = [row for row in rows if row["expected_selected"] == "TRUE"]
    excluded = [row for row in rows if row["expected_eligible"] == "FALSE"]
    assert len(rows) == 43, "expected 43 candidate rows"
    assert len(ids) == 15 and ids == catalog_ids, "scenario IDs do not match catalog"
    assert len(keys) == len(set(keys)), "scenario_id + candidate_id must be unique"
    assert len(selected) == 13, "expected 13 selected rows"
    assert len(excluded) == 15, "expected 15 excluded rows"
    assert all(row["expected_eligible"] == "TRUE" for row in selected)
    for scenario in scenarios:
        count = sum(row["expected_selected"] == "TRUE" for row in rows if row["scenario_id"] == scenario["scenario_id"])
        assert count == (0 if scenario["expected_status"] == "unroutable" else 1)


def build(output: Path | None = DEFAULT_OUTPUT, dry_run: bool = False) -> list[dict[str, str]]:
    candidates = read_csv(DEFAULT_CANDIDATES)
    scenarios = read_csv(DEFAULT_SCENARIOS)
    rows = [row for scenario in scenarios for row in scenario_rows(scenario, candidates)]
    validate(rows, scenarios)
    if not dry_run:
        assert output is not None
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate without writing")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="output CSV path")
    args = parser.parse_args()
    rows = build(args.output, args.dry_run)
    print(f"generated and validated {len(rows)} rows" + (" (dry-run)" if args.dry_run else f" -> {args.output}"))


if __name__ == "__main__":
    main()
