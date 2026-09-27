"""Real-evidence aggregation for the observability overview."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from backend.call_log_service import CallLogService, _percentile


FAILURE_STATES = {"FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED"}


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


class ObservabilityOverviewService:
    def __init__(self, logs: CallLogService):
        self.logs = logs

    @staticmethod
    def _window(time_range: str, start: str | None, end: str | None) -> tuple[datetime, datetime]:
        now = datetime.now(timezone.utc)
        if time_range == "custom":
            start_at, end_at = _parse(start), _parse(end)
            if not start_at or not end_at or start_at >= end_at:
                raise ValueError("invalid_custom_time_range")
            return start_at, end_at
        durations = {"5m": timedelta(minutes=5), "1h": timedelta(hours=1),
                     "24h": timedelta(hours=24), "7d": timedelta(days=7)}
        if time_range not in durations:
            raise ValueError("invalid_time_range")
        return now - durations[time_range], now

    def overview(self, *, environment_id: str = "china_uat", time_range: str = "24h",
                 start: str | None = None, end: str | None = None,
                 traffic_class: str = "business", model_id: str | None = None,
                 stream: bool | None = None, source_type: str = "all",
                 circuits: dict[str, Any] | None = None) -> dict[str, Any]:
        start_at, end_at = self._window(time_range, start, end)
        clauses = ["tenant_id=?", "workspace_id=?", "environment_id=?",
                   "occurred_at>=?", "occurred_at<=?", "request_status<>'RUNNING'",
                   "duplicate_status<>'exact_duplicate'"]
        params: list[Any] = [*self.logs.scope.sql_parameters(), environment_id,
                             _iso(start_at), _iso(end_at)]
        if traffic_class == "business":
            clauses.append("COALESCE(traffic_class,'business')<>'probe'")
        elif traffic_class == "probe":
            clauses.append("traffic_class='probe'")
        elif traffic_class != "all":
            raise ValueError("traffic_class_invalid")
        if model_id:
            clauses.append("COALESCE(actual_model,requested_model)=?"); params.append(model_id)
        if stream is not None:
            clauses.append("stream=?"); params.append(int(stream))
        if source_type == "historical":
            clauses.append("is_historical=1")
        elif source_type == "realtime":
            clauses.append("source_type='realtime_execution'")
        elif source_type != "all":
            raise ValueError("source_type_invalid")
        where = " AND ".join(clauses)
        duration = end_at - start_at
        previous_start, previous_end = start_at - duration, start_at
        previous_clauses = [item for item in clauses if not item.startswith("occurred_at")]
        previous_where = " AND ".join(previous_clauses + ["occurred_at>=?", "occurred_at<?"])
        filter_params = params[0:3] + params[5:]
        with self.logs.connect() as db:
            rows = [dict(row) for row in db.execute(
                f"SELECT * FROM standardized_call_logs WHERE {where} ORDER BY occurred_at", params)]
            previous = [dict(row) for row in db.execute(
                f"SELECT * FROM standardized_call_logs WHERE {previous_where}",
                [*filter_params, _iso(previous_start), _iso(previous_end)])]
            models = [str(row[0]) for row in db.execute("""SELECT DISTINCT
              COALESCE(actual_model,requested_model) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              AND duplicate_status<>'exact_duplicate' ORDER BY 1""",
              (*self.logs.scope.sql_parameters(), environment_id))]
            source_counts = {str(row[0]): int(row[1]) for row in db.execute("""SELECT source_type,COUNT(*)
              FROM standardized_call_logs WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              AND duplicate_status<>'exact_duplicate' GROUP BY source_type""",
              (*self.logs.scope.sql_parameters(), environment_id))}
            watermark = db.execute("""SELECT MAX(cursor_id),MAX(updated_at) FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?""",
              (*self.logs.scope.sql_parameters(), environment_id)).fetchone()
        return self._project(rows, previous, start_at, end_at, environment_id, time_range,
                             traffic_class, model_id, stream, source_type, models,
                             source_counts, watermark, circuits or {})

    def _project(self, rows: list[dict], previous: list[dict], start_at: datetime, end_at: datetime,
                 environment_id: str, time_range: str, traffic_class: str,
                 model_id: str | None, stream: bool | None, source_type: str,
                 models: list[str], source_counts: dict[str, int], watermark: Any,
                 circuits: dict[str, Any]) -> dict[str, Any]:
        def metrics(items: list[dict]) -> dict[str, Any]:
            latency = [float(r["total_latency_ms"]) for r in items if r["total_latency_ms"] is not None]
            ttft = [float(r["first_token_latency_ms"]) for r in items if r["first_token_latency_ms"] is not None]
            successes = sum(r["request_status"] == "SUCCESS" for r in items)
            actual = [Decimal(str(r["cost_amount"])) for r in items if r["cost_amount"] is not None and
                      (r["is_historical"] or r["cost_status"] == "actual_provider_cost" or
                       r["cost_type"] == "actual_provider_cost")]
            return {"requests": len(items), "success_rate": successes / len(items) if items else None,
                    "p50_ms": _percentile(latency, .5), "p95_ms": _percentile(latency, .95),
                    "p99_ms": _percentile(latency, .99), "ttft_p95_ms": _percentile(ttft, .95),
                    "tokens": sum(int(r["input_tokens"] or 0)+int(r["output_tokens"] or 0) for r in items),
                    "actual_cost": str(sum(actual, Decimal("0"))) if actual else None,
                    "latency_coverage": len(latency), "ttft_coverage": len(ttft),
                    "token_coverage": sum(any(r[k] is not None for k in ("input_tokens","output_tokens")) for r in items),
                    "cost_coverage": len(actual)}
        current, prior = metrics(rows), metrics(previous)
        deltas = {}
        for key in ("requests", "success_rate", "p95_ms", "ttft_p95_ms", "tokens"):
            deltas[key] = None if current[key] is None or prior[key] in (None, 0) else (current[key]-prior[key])/prior[key]
        bucket_seconds = 60 if time_range in {"5m","1h"} else 3600 if time_range == "24h" else 86400
        buckets: dict[datetime, list[dict]] = defaultdict(list)
        for row in rows:
            point = _parse(str(row["occurred_at"])) or start_at
            epoch = int(point.timestamp()) // bucket_seconds * bucket_seconds
            buckets[datetime.fromtimestamp(epoch, timezone.utc)].append(row)
        request_series=[]; latency_series=[]; token_series=[]; cost_series=[]
        for point, items in sorted(buckets.items()):
            lat=[float(r["total_latency_ms"]) for r in items if r["total_latency_ms"] is not None]
            ttft=[float(r["first_token_latency_ms"]) for r in items if r["first_token_latency_ms"] is not None]
            provider=Decimal("0");historical=Decimal("0");estimated=Decimal("0");pending=0
            for row in items:
                amount=Decimal(str(row["cost_amount"])) if row["cost_amount"] is not None else None
                if row["is_historical"] and amount is not None: historical+=amount
                elif row["cost_status"]=="actual_provider_cost" and amount is not None: provider+=amount
                elif row["cost_type"]=="estimated_versioned_price" and amount is not None: estimated+=amount
                elif row["cost_status"]=="pending_provider_sync": pending+=1
            request_series.append({"time":_iso(point),"business":sum((r["traffic_class"] or "business")!="probe" for r in items),
              "probe":sum(r["traffic_class"]=="probe" for r in items),"requests":len(items),
              "success_rate":sum(r["request_status"]=="SUCCESS" for r in items)/len(items)})
            latency_series.append({"time":_iso(point),"p50_ms":_percentile(lat,.5),"p95_ms":_percentile(lat,.95),
              "p99_ms":_percentile(lat,.99),"ttft_p95_ms":_percentile(ttft,.95)})
            token_series.append({"time":_iso(point),"input_tokens":sum(int(r["input_tokens"] or 0) for r in items),
              "cached_tokens":sum(int(r["cached_input_tokens"] or 0) for r in items),
              "output_tokens":sum(int(r["output_tokens"] or 0) for r in items)})
            cost_series.append({"time":_iso(point),"provider_actual":str(provider) if provider else None,
              "historical_actual":str(historical) if historical else None,
              "estimated":str(estimated) if estimated else None,"pending":pending})
        model_groups: dict[str,list[dict]]=defaultdict(list)
        for row in rows:model_groups[str(row["actual_model"] or row["requested_model"])].append(row)
        model_distribution=[]
        for name,items in sorted(model_groups.items(),key=lambda pair:-len(pair[1]))[:10]:
            mm=metrics(items); model_distribution.append({"model_id":name,"request_count":len(items),
              "success_rate":mm["success_rate"],"p95_ms":mm["p95_ms"],"tokens":mm["tokens"],
              "cost":mm["actual_cost"],"source_types":sorted({r["source_type"] for r in items})})
        error_distribution=[{"code":code,"count":count} for code,count in Counter(
            str(r["error_category"] or r["error_code"]) for r in rows if r["error_category"] or r["error_code"]).most_common()]
        provider_actual=Decimal("0");historical_actual=Decimal("0");estimated=Decimal("0");pending=0
        for row in rows:
            amount=Decimal(str(row["cost_amount"])) if row["cost_amount"] is not None else None
            if row["is_historical"] and amount is not None:historical_actual+=amount
            elif row["cost_status"]=="actual_provider_cost" and amount is not None:provider_actual+=amount
            elif row["cost_type"]=="estimated_versioned_price" and amount is not None:estimated+=amount
            elif row["cost_status"]=="pending_provider_sync":pending+=1
        retries=sum(max(0,int(r["total_attempts"] or 1)-1) for r in rows)
        fallbacks=sum(bool(r["fallback_used"] or int(r["total_attempts"] or 1)>1) for r in rows)
        counts=circuits.get("state_counts") or {}
        return {"scope":{"environment_id":environment_id,"time_range":time_range,"start":_iso(start_at),
          "end":_iso(end_at),"traffic_class":traffic_class,"model_id":model_id,"stream":stream,"source_type":source_type},
          "filters":{"models":models},"kpis":{**current,"deltas":deltas,
            "provider_actual_cost":str(provider_actual) if provider_actual else None,
            "historical_actual_cost":str(historical_actual) if historical_actual else None,
            "estimated_cost":str(estimated) if estimated else None,"pending_cost_count":pending},
          "request_series":request_series,"latency_series":latency_series,"token_series":token_series,
          "cost_series":cost_series,"model_distribution":model_distribution,
          "stream_distribution":{"stream":sum(bool(r["stream"]) for r in rows),
            "nonstream_completion":sum(not r["stream"] and (r["endpoint_type"] or "").lower() not in {"models","health"} for r in rows),
            "non_completion":sum((r["endpoint_type"] or "").lower() in {"models","health"} for r in rows)},
          "error_distribution":error_distribution,
          "reliability":{"fallback_rate":fallbacks/len(rows) if rows else None,
            "retry_rate":retries/len(rows) if rows else None,"open_count":int(counts.get("OPEN",0)),
            "half_open_count":int(counts.get("HALF_OPEN",0)),"active_probes":int(circuits.get("active_probe_leases",0)),
            "timeline":circuits.get("items",[])[:8]},
          "freshness":{"watermark":watermark[0] if watermark else None,"last_synced_at":watermark[1] if watermark else None,
            "source_counts":source_counts,"provider_log_status":"partial" if pending else "ready"},
          "coverage":{"records":len(rows),"latency":current["latency_coverage"],"ttft":current["ttft_coverage"],
            "tokens":current["token_coverage"],"actual_cost":current["cost_coverage"]},
          "recent_records":[{"occurred_at":r["occurred_at"],"traffic_class":r["traffic_class"] or "business",
            "model_id":r["actual_model"] or r["requested_model"],"status":r["request_status"],
            "total_latency_ms":r["total_latency_ms"],"first_token_ms":r["first_token_latency_ms"],
            "tokens":int(r["input_tokens"] or 0)+int(r["output_tokens"] or 0),"cost":r["cost_amount"],
            "cost_type":r["cost_type"] or r["cost_status"],"request_id":r["local_request_id"] or r["request_id"],
            "decision_id":r["decision_id"],"error_category":r["error_category"]} for r in reversed(rows[-30:])],
          "generated_at":datetime.now(timezone.utc).isoformat()}
