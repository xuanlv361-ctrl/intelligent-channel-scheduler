"""Read-only application projection for Scheduler timing telemetry."""

from __future__ import annotations

import sys
from pathlib import Path
import hashlib
import json
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scheduler_timing import (PERFORMANCE_STAGES, SchedulerTimingRecorder,
                              _percentile)  # noqa: E402


class SchedulerOverheadService:
    def __init__(self, database_path: str | Path,
                 budget_path: str | Path | None = None):
        self.recorder = SchedulerTimingRecorder(database_path)
        self.budget_path = Path(budget_path or
            ROOT / "config" / "scheduler_performance_budget_v1.json")
        self.budget = json.loads(self.budget_path.read_text(encoding="utf-8"))
        if (self.budget.get("provider_latency_must_be_excluded") is not True
                or set(self.budget.get("stages") or {}) != {
                    "candidate_loading", "metric_lookup", "eligibility_and_scoring",
                    "decision_persistence", "audit_emission", "scheduler_total"}
                or int(self.budget.get("minimum_total_samples", 0)) < 1):
            raise ValueError("invalid_scheduler_performance_budget")
        self.budget_sha256 = hashlib.sha256(
            self.budget_path.read_bytes()).hexdigest().upper()

    def status(self, *, window_minutes: int = 60) -> dict:
        result = self.recorder.summary(window_minutes=window_minutes)
        assessments = []
        for stage in result["stages"]:
            limits = self.budget["stages"][stage["stage"]]
            measured = stage["p95_ms"] is not None and stage["p99_ms"] is not None
            passed = (measured and stage["p95_ms"] <= limits["p95_ms"]
                      and stage["p99_ms"] <= limits["p99_ms"])
            assessments.append({
                "stage": stage["stage"], "sample_count": stage["sample_count"],
                "status": "pass" if passed else "fail" if measured else "unmeasured",
                "p95_budget_ms": limits["p95_ms"],
                "p99_budget_ms": limits["p99_ms"],
            })
        total = next(item for item in result["stages"]
                     if item["stage"] == "scheduler_total")
        enough = total["sample_count"] >= int(self.budget["minimum_total_samples"])
        budget_status = ("insufficient_samples" if not enough else
                         "pass" if all(item["status"] == "pass"
                                       for item in assessments) else "fail")
        result.update({
            "metric_definition": "scheduler_compute_and_local_persistence_only",
            "excluded_time": [
                "provider_network_latency",
                "streaming_generation_time",
                "client_rendering_time",
            ],
            "percentiles": ["p50", "p95", "p99"],
            "performance_budget": {
                "status": budget_status,
                "policy_version": self.budget["policy_version"],
                "policy_sha256": self.budget_sha256,
                "minimum_total_samples": self.budget["minimum_total_samples"],
                "assessments": assessments,
            },
        })
        return result

    def performance(self, *, environment_id: str = "china_uat",
                    window_minutes: int = 1440, strategy_id: str | None = None,
                    model_id: str | None = None, traffic_class: str = "business",
                    stream: bool | None = None, start: str | None = None,
                    end: str | None = None) -> dict:
        now = datetime.now(timezone.utc)
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else now-timedelta(minutes=window_minutes)
        end_at = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else now
        clauses=["environment_id=?","traffic_class=?","occurred_at>=?","occurred_at<=?",
                 "source_type IN ('realtime_execution','reconstructed_from_decision_events')"]
        params:list[object]=[environment_id,traffic_class,start_at.isoformat(),end_at.isoformat()]
        if strategy_id:
            clauses.append("strategy_id=?");params.append(strategy_id)
        if model_id:
            clauses.append("model_id=?");params.append(model_id)
        if stream is not None:
            clauses.append("stream=?");params.append(1 if stream else 0)
        where=" AND ".join(clauses)
        with self.recorder._connect() as db:
            records=[dict(row) for row in db.execute(
                f"SELECT * FROM scheduler_performance_requests WHERE {where} ORDER BY occurred_at",params)]
            span_rows=[dict(row) for row in db.execute(
                f"""SELECT s.* FROM scheduler_performance_spans s JOIN scheduler_performance_requests r
                ON r.local_request_id=s.local_request_id WHERE {where.replace('environment_id','r.environment_id').replace('traffic_class','r.traffic_class').replace('occurred_at','r.occurred_at').replace('strategy_id','r.strategy_id').replace('model_id','r.model_id').replace('stream','r.stream').replace('source_type','r.source_type')}
                ORDER BY r.occurred_at,s.stage""",params)]
            latest=db.execute("""SELECT occurred_at FROM scheduler_performance_requests
              WHERE environment_id=? AND traffic_class=? ORDER BY occurred_at DESC LIMIT 1""",
              (environment_id,traffic_class)).fetchone()
        totals=[float(row["scheduler_total_ms"]) for row in records]
        providers=[float(row["provider_latency_ms"]) for row in records if row["provider_latency_ms"] is not None]
        end_to_end=[float(row["end_to_end_ms"]) for row in records]
        by_stage={stage:[] for stage in PERFORMANCE_STAGES}
        for row in span_rows: by_stage.setdefault(row["stage"],[]).append(float(row["duration_ms"]))
        stage_distribution=[]
        measured_stage_total=sum(sum(values) for stage,values in by_stage.items() if stage!="scheduler_total")
        for stage in PERFORMANCE_STAGES:
            if stage=="scheduler_total":continue
            values=by_stage.get(stage,[])
            stage_distribution.append({"stage":stage,"average_ms":sum(values)/len(values) if values else None,
              "p95_ms":_percentile(values,.95),"sample_count":len(values),
              "share":(sum(values)/measured_stage_total if measured_stage_total and values else None)})
        largest=max((row for row in stage_distribution if row["average_ms"] is not None),
                    key=lambda row:float(row["average_ms"]),default=None)
        stage_by_request:dict[str,dict[str,float]]={}
        for row in span_rows:stage_by_request.setdefault(row["local_request_id"],{})[row["stage"]]=row["duration_ms"]
        stage_series=[{"time":row["occurred_at"],"request_id":row["local_request_id"],
          **stage_by_request.get(row["local_request_id"],{})} for row in records]
        latency_series=[]
        for row in records:
            upto=[float(item["scheduler_total_ms"]) for item in records if item["occurred_at"]<=row["occurred_at"]]
            latency_series.append({"time":row["occurred_at"],"p50_ms":_percentile(upto,.5),
              "p95_ms":_percentile(upto,.95),"p99_ms":_percentile(upto,.99)})
        strategy_comparison=[]
        for name in sorted({str(row["strategy_id"]) for row in records}):
            subset=[row for row in records if row["strategy_id"]==name]
            values=[float(row["scheduler_total_ms"]) for row in subset]
            strategy_comparison.append({"strategy_id":name,"sample_count":len(subset),
              "p50_ms":_percentile(values,.5),"p95_ms":_percentile(values,.95),
              "average_candidate_count":self._average(subset,"candidate_count"),
              "average_filtered_count":self._average(subset,"filtered_count"),
              "average_scoring_ms":self._average_stage(subset,stage_by_request,"scoring")})
        ratio=(sum(totals)/sum(end_to_end)) if end_to_end and sum(end_to_end)>0 else None
        return {"scope":{"environment_id":environment_id,"window_minutes":window_minutes,
                  "start":start_at.isoformat(),"end":end_at.isoformat(),"traffic_class":traffic_class,
                  "strategy_id":strategy_id,"model_id":model_id,"stream":stream},
          "kpis":{"request_count":len(records),"scheduler_p50_ms":_percentile(totals,.5),
                  "scheduler_p95_ms":_percentile(totals,.95),"scheduler_p99_ms":_percentile(totals,.99),
                  "scheduler_end_to_end_ratio":ratio,"largest_stage":largest["stage"] if largest else None},
          "stage_series":stage_series,"latency_series":latency_series,
          "stage_distribution":stage_distribution,
          "provider_comparison":[{"time":row["occurred_at"],"request_id":row["local_request_id"],
             "scheduler_ms":row["scheduler_total_ms"],"provider_ms":row["provider_latency_ms"],
             "end_to_end_ms":row["end_to_end_ms"]} for row in records],
          "strategy_comparison":strategy_comparison,
          "recent_records":[{**row,"stage_durations":stage_by_request.get(row["local_request_id"],{})}
                            for row in reversed(records[-100:])],
          "freshness":{"generated_at":now.isoformat(),"latest_record_at":latest[0] if latest else None},
          "coverage":{"scheduler_samples":len(totals),"provider_samples":len(providers),
             "end_to_end_samples":len(end_to_end),"source_types":sorted({row["source_type"] for row in records})}}

    @staticmethod
    def _average(rows: list[dict], key: str) -> float | None:
        values=[float(row[key]) for row in rows if row.get(key) is not None]
        return sum(values)/len(values) if values else None

    @staticmethod
    def _average_stage(rows: list[dict], stages: dict[str,dict[str,float]], stage: str) -> float | None:
        values=[float(stages.get(row["local_request_id"],{}).get(stage)) for row in rows
                if stages.get(row["local_request_id"],{}).get(stage) is not None]
        return sum(values)/len(values) if values else None
