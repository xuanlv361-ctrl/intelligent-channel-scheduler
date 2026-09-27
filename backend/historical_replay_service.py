"""Tenant-scoped historical replay over the real standardized UAT call ledger.

The service never substitutes demo rows and never calls a provider.  Historical
actuals are preserved separately from counterfactual estimates; unavailable
price, channel, or latency evidence remains ``None`` instead of becoming zero.
"""
from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from backend.tenant_security import TenantScope

REAL_SOURCES = ("historical_uat_csv", "realtime_execution")
TERMINAL_STATES = ("SUCCESS", "FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED")
STRATEGIES = ("actual_observed", "latency_first", "cost_first")
DISCLAIMER = (
    "候选策略结果为基于真实历史请求的离线估算，不代表已经获得实际收益。"
    "实际效果需要通过影子调度或真实UAT实验验证。"
)


class HistoricalReplayError(ValueError):
    """Stable, non-sensitive failure code."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


class HistoricalReplayService:
    schema_version = "historical_replay_v1"
    minimum_latency_samples = 3

    def __init__(self, path: str | Path, scope: TenantScope | None = None):
        self.path = Path(path)
        self.scope = scope or TenantScope.local_development()
        self._migrate()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _migrate(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS historical_replay_runs(
              replay_id TEXT NOT NULL, schema_version TEXT NOT NULL,
              environment_id TEXT NOT NULL, baseline_strategy TEXT NOT NULL,
              candidate_strategy TEXT NOT NULL, filters_json TEXT NOT NULL,
              source_watermark TEXT, sample_count INTEGER NOT NULL,
              exact_association_count INTEGER NOT NULL,
              unlinked_count INTEGER NOT NULL, result_json TEXT NOT NULL,
              created_at TEXT NOT NULL, tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,replay_id));
            CREATE TABLE IF NOT EXISTS historical_replay_items(
              replay_id TEXT NOT NULL, record_id TEXT NOT NULL,
              occurred_at TEXT NOT NULL, request_id TEXT, response_id TEXT,
              decision_id TEXT, actual_json TEXT NOT NULL,
              counterfactual_json TEXT NOT NULL, timeline_json TEXT NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,replay_id,record_id),
              FOREIGN KEY(tenant_id,workspace_id,replay_id)
                REFERENCES historical_replay_runs(tenant_id,workspace_id,replay_id));
            CREATE INDEX IF NOT EXISTS ix_replay_items_scope_time ON
              historical_replay_items(tenant_id,workspace_id,replay_id,occurred_at);
            """)
            existing = {row[1] for row in db.execute("PRAGMA table_info(historical_replay_items)")}
            for name in ("source_type", "association", "configuration_version", "metric_snapshot_id"):
                if name not in existing:
                    db.execute(f"ALTER TABLE historical_replay_items ADD COLUMN {name} TEXT")

    def _where(self, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["tenant_id=?", "workspace_id=?", "environment_id=?",
                   "source_type IN (?,?)", "request_status IN (?,?,?,?,?)",
                   "COALESCE(traffic_class,'business')<>'probe'"]
        params: list[Any] = [*self.scope.sql_parameters(),
                             filters.get("environment_id", "china_uat"),
                             *REAL_SOURCES, *TERMINAL_STATES]
        mapping = {"occurred_from": "occurred_at>=?", "occurred_to": "occurred_at<=?",
                   "model": "requested_model=?", "channel": "channel_id=?",
                   "request_type": "endpoint_type=?"}
        for key, clause in mapping.items():
            value = filters.get(key)
            if value:
                clauses.append(clause)
                params.append(str(value))
        if filters.get("exact_only"):
            clauses.extend(["request_id IS NOT NULL", "decision_id IS NOT NULL"])
        return " AND ".join(clauses), params

    def source_summary(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = dict(filters or {})
        where, params = self._where(filters)
        with self.connect() as db:
            row = db.execute(f"""SELECT COUNT(*) AS n,MIN(occurred_at) AS first_at,
              MAX(occurred_at) AS last_at,MAX(updated_at) AS last_sync,
              MAX(cursor_id) AS watermark,
              SUM(CASE WHEN request_id IS NOT NULL AND decision_id IS NOT NULL
                  THEN 1 ELSE 0 END) AS exact_n
              FROM standardized_call_logs WHERE {where}""", params).fetchone()
            sources = [dict(item) for item in db.execute(f"""SELECT source_type,
              COUNT(*) AS count FROM standardized_call_logs WHERE {where}
              GROUP BY source_type ORDER BY source_type""", params).fetchall()]
            versions = [item[0] for item in db.execute(f"""SELECT DISTINCT
              configuration_version FROM standardized_call_logs WHERE {where}
              AND configuration_version IS NOT NULL ORDER BY configuration_version""",
              params).fetchall()]
            price_versions = [item[0] for item in db.execute("""SELECT DISTINCT
              catalog_version FROM price_catalog_versions WHERE tenant_id=? AND
              workspace_id=? ORDER BY created_at DESC LIMIT 20""",
              self.scope.sql_parameters()).fetchall()] if self._table(db, "price_catalog_versions") else []
            measurements = [dict(item) for item in db.execute(f"""SELECT actual_model,
              requested_model,channel_id,stream,request_status,total_latency_ms,
              input_tokens,cached_input_tokens,output_tokens,cost_amount,currency,
              source_type,request_id,decision_id,evidence_level FROM standardized_call_logs
              WHERE {where}""", params).fetchall()]
        count = int(row["n"] or 0)
        exact = int(row["exact_n"] or 0)
        latencies = [float(item["total_latency_ms"]) for item in measurements
                     if item["total_latency_ms"] is not None]
        costs = [_decimal(item["cost_amount"]) for item in measurements]
        observed_costs = [item for item in costs if item is not None]
        historical_statistics = sum(
            item["source_type"] == "historical_uat_csv" and not (
                item["request_id"] and item["decision_id"]
            ) for item in measurements
        )
        terminal_failures = {"FAILED", "TIMEOUT", "INTERRUPTED", "CANCELLED"}
        actual = {
            "record_count": count,
            "input_tokens": sum(int(item["input_tokens"] or 0) for item in measurements),
            "cached_input_tokens": sum(int(item["cached_input_tokens"] or 0) for item in measurements),
            "output_tokens": sum(int(item["output_tokens"] or 0) for item in measurements),
            "total_cost": format(sum(observed_costs, Decimal("0")), "f") if observed_costs else None,
            "average_cost": format(sum(observed_costs, Decimal("0")) / len(observed_costs), "f") if observed_costs else None,
            "cost_coverage_count": len(observed_costs),
            "p50_latency_ms": _percentile(latencies, .50),
            "p95_latency_ms": _percentile(latencies, .95),
            "p99_latency_ms": _percentile(latencies, .99),
            "failure_count": sum(item["request_status"] in terminal_failures for item in measurements),
            "stream_count": sum(bool(item["stream"]) for item in measurements),
            "nonstream_count": sum(not bool(item["stream"]) for item in measurements),
            "model_count": len({str(item["actual_model"] or item["requested_model"])
                                for item in measurements}),
            "currency": "CNY" if observed_costs else None,
            "price_source": "historical_log_detail" if any(
                item["source_type"] == "historical_uat_csv" for item in measurements) else None,
            "price_version": "unversioned_historical_snapshot" if any(
                item["source_type"] == "historical_uat_csv" for item in measurements) else None,
        }
        if actual["price_version"] and actual["price_version"] not in price_versions:
            price_versions.append(actual["price_version"])
        return {"schema_version": self.schema_version, "environment_id": filters.get("environment_id", "china_uat"),
                "source_type": "real_uat_call_logs" if count else None, "is_mock": False,
                "sample_count": count, "exact_association_count": exact,
                "unlinked_count": count - exact, "occurred_from": row["first_at"],
                "occurred_to": row["last_at"], "last_synced_at": row["last_sync"],
                "source_watermark": str(row["watermark"]) if row["watermark"] is not None else None,
                "sources": sources, "configuration_versions": versions,
                "price_versions": price_versions, "actual": actual,
                "evidence_capabilities": {
                    "historical_aggregation": count,
                    "exact_decision_reconstruction": exact,
                    "historical_statistics_only": historical_statistics,
                    "authoritative_channel": sum(bool(item["channel_id"]) for item in measurements),
                    "unusable": 0,
                },
                "empty_message": None if count else "当前没有可用于回放的真实UAT日志。"}

    @staticmethod
    def _table(db: sqlite3.Connection, name: str) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _rows(self, filters: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        where, params = self._where(filters)
        with self.connect() as db:
            return [dict(row) for row in db.execute(f"""SELECT * FROM standardized_call_logs
              WHERE {where} ORDER BY occurred_at DESC,cursor_id DESC LIMIT ?""",
              (*params, limit)).fetchall()]

    def _prices(self, db: sqlite3.Connection) -> list[dict[str, Any]]:
        if not self._table(db, "price_catalog_records"):
            return []
        return [dict(row) for row in db.execute("""SELECT * FROM price_catalog_records
          WHERE tenant_id=? AND workspace_id=? ORDER BY effective_from DESC""",
          self.scope.sql_parameters()).fetchall()]

    @staticmethod
    def _matching_price(prices: list[dict[str, Any]], row: dict[str, Any],
                        model: str, channel: str) -> dict[str, Any] | None:
        occurred = str(row["occurred_at"])
        for price in prices:
            if (price["environment_id"] == row["environment_id"] and
                    price["model_id"] == model and price["channel_id"] == channel and
                    price["effective_from"] <= occurred and
                    (price["effective_until"] is None or occurred < price["effective_until"])):
                return price
        return None

    @staticmethod
    def _estimate_cost(row: dict[str, Any], price: dict[str, Any] | None) -> tuple[str | None, str | None]:
        if price is None:
            return None, "price_version_missing"
        if price["billing_unit"] not in {"per_1m_tokens", "1m_tokens"}:
            return None, "unsupported_billing_unit"
        input_tokens = row.get("input_tokens")
        output_tokens = row.get("output_tokens")
        if input_tokens is None or output_tokens is None:
            return None, "token_evidence_missing"
        cached = int(row.get("cached_input_tokens") or 0)
        regular_input = max(0, int(input_tokens) - cached)
        cached_price = _decimal(price.get("cached_input_unit_price"))
        input_price = _decimal(price.get("input_unit_price"))
        output_price = _decimal(price.get("output_unit_price"))
        if input_price is None or output_price is None:
            return None, "invalid_price_record"
        total = (Decimal(regular_input) * input_price + Decimal(int(output_tokens)) * output_price) / Decimal(1_000_000)
        if cached and cached_price is None:
            return None, "cached_input_price_missing"
        if cached_price is not None:
            total += Decimal(cached) * cached_price / Decimal(1_000_000)
        return format(total, "f"), None

    def run(self, *, baseline_strategy: str, candidate_strategy: str,
            filters: dict[str, Any] | None = None, limit: int = 500) -> dict[str, Any]:
        if baseline_strategy not in STRATEGIES or candidate_strategy not in STRATEGIES:
            raise HistoricalReplayError("unsupported_replay_strategy")
        if baseline_strategy == candidate_strategy:
            raise HistoricalReplayError("baseline_candidate_must_differ")
        if not 1 <= int(limit) <= 1000:
            raise HistoricalReplayError("invalid_replay_limit")
        filters = dict(filters or {})
        filters.setdefault("environment_id", "china_uat")
        rows = self._rows(filters, int(limit))
        source = self.source_summary(filters)
        replay_id = "HR-" + uuid.uuid4().hex.upper()
        with self.connect() as db:
            prices = self._prices(db)
        aggregates: dict[tuple[str, str], dict[str, Any]] = {}
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            model = str(row.get("actual_model") or row.get("requested_model") or "")
            channel = row.get("channel_id")
            if model and channel:
                grouped[(model, str(channel))].append(row)
        for key, members in grouped.items():
            latencies = [float(item["total_latency_ms"]) for item in members if item.get("total_latency_ms") is not None]
            costs = [_decimal(item.get("cost_amount")) for item in members]
            valid_costs = [item for item in costs if item is not None]
            aggregates[key] = {"sample_count": len(members), "latency_sample_count": len(latencies),
                               "p95_latency_ms": _percentile(latencies, .95),
                               "average_cost": (sum(valid_costs) / len(valid_costs)) if valid_costs else None,
                               "cost_sample_count": len(valid_costs)}
        items: list[dict[str, Any]] = []
        for row in rows:
            model = str(row.get("actual_model") or row.get("requested_model") or "")
            original_channel = row.get("channel_id")
            candidates = [(key, value) for key, value in aggregates.items() if key[0] == model]
            candidate_key: tuple[str, str] | None = None
            reason: str | None = None
            if candidate_strategy == "latency_first":
                eligible = [(key, data) for key, data in candidates if data["latency_sample_count"] >= self.minimum_latency_samples]
                candidate_key = min(eligible, key=lambda item: (item[1]["p95_latency_ms"], item[0][1]))[0] if eligible else None
                reason = None if candidate_key else "insufficient_candidate_latency_samples"
            elif candidate_strategy == "cost_first":
                eligible = [(key, data) for key, data in candidates if data["average_cost"] is not None]
                candidate_key = min(eligible, key=lambda item: (item[1]["average_cost"], item[0][1]))[0] if eligible else None
                reason = None if candidate_key else "candidate_cost_evidence_missing"
            else:
                candidate_key = (model, str(original_channel)) if original_channel else None
                reason = None if candidate_key else "authoritative_channel_missing"
            if (candidate_key is None and row.get("source_type") == "historical_uat_csv"
                    and not row.get("request_id")):
                # The CSV is valid observed usage evidence, but it contains no
                # request body/capability requirements.  That prevents a
                # request-level model reselection; it does not invalidate the
                # observed Token, latency or cost measurements.
                reason = "historical_request_content_missing"
            candidate_channel = candidate_key[1] if candidate_key else None
            stats = aggregates.get(candidate_key) if candidate_key else None
            price = self._matching_price(prices, row, model, candidate_channel) if candidate_channel else None
            estimated_cost, cost_reason = self._estimate_cost(row, price)
            actual_cost = row.get("cost_amount")
            cost_delta = None
            if _decimal(actual_cost) is not None and _decimal(estimated_cost) is not None:
                cost_delta = format(_decimal(estimated_cost) - _decimal(actual_cost), "f")
            actual = {"model": model or None,
                      "requested_model": row.get("requested_model"),
                      "billed_model": row.get("billed_model") or row.get("requested_model"),
                      "actual_model": row.get("actual_model") or row.get("requested_model"),
                      "channel_id": original_channel,
                      "channel_name": row.get("channel_name"), "status": row.get("request_status"),
                      "http_status": row.get("http_status"), "latency_ms": row.get("total_latency_ms"),
                      "first_token_latency_ms": row.get("first_token_latency_ms"),
                      "stream": bool(row.get("stream")),
                      "duration_type": row.get("duration_type"),
                      "cost_amount": actual_cost, "currency": row.get("currency"),
                      "pricing_detail": row.get("pricing_detail"),
                      "price_source": row.get("price_source"),
                      "price_version": row.get("price_version"),
                      "evidence_level": row.get("evidence_level"),
                      "error_code": row.get("error_code"), "error_category": row.get("error_category"),
                      "input_tokens": row.get("input_tokens"), "cached_input_tokens": row.get("cached_input_tokens"),
                      "output_tokens": row.get("output_tokens"), "total_attempts": row.get("total_attempts"),
                      "fallback": bool((row.get("total_attempts") or 1) > 1), "label": "历史实际"}
            counterfactual = {"strategy": candidate_strategy, "model": candidate_key[0] if candidate_key else None,
                              "channel_id": candidate_channel, "choice_changed": None if candidate_key is None or original_channel is None else candidate_channel != original_channel,
                              "estimated_cost": estimated_cost, "cost_delta": cost_delta,
                              "currency": price.get("currency") if price else row.get("currency"),
                              "price_version": price.get("catalog_version") if price else None,
                              "cost_basis": None if estimated_cost is None else "historical_tokens_x_versioned_unit_price",
                              "cost_unavailable_reason": cost_reason,
                              "estimated_latency_ms": stats.get("p95_latency_ms") if stats and stats["latency_sample_count"] >= self.minimum_latency_samples else None,
                              "latency_sample_count": stats.get("latency_sample_count", 0) if stats else 0,
                              "latency_basis": "matching_model_channel_historical_p95" if stats and stats["latency_sample_count"] >= self.minimum_latency_samples else None,
                              "confidence": "high" if stats and stats["latency_sample_count"] >= 20 else "medium" if stats and stats["latency_sample_count"] >= self.minimum_latency_samples else "low",
                              "reason": reason, "label": "离线估算"}
            exact = bool(row.get("request_id") and row.get("decision_id"))
            association = ("exact" if exact else "historical_statistics"
                           if row.get("source_type") == "historical_uat_csv" else "execution_only")
            actual["evidence_level"] = row.get("evidence_level") or association
            timeline = [
                {"step": 1, "title": "输入请求摘要", "status": "available", "evidence": {"endpoint_type": row.get("endpoint_type"), "stream": bool(row.get("stream")), "requested_model": row.get("requested_model")}},
                {"step": 2, "title": "当时的指标快照", "status": "available" if row.get("metric_snapshot_id") else "missing", "evidence": {"metric_snapshot_id": row.get("metric_snapshot_id")}},
                {"step": 3, "title": "基线策略候选集合", "status": "available" if original_channel else "missing", "evidence": {"selected_model": model or None, "selected_channel": original_channel, "association": association}},
                {"step": 4, "title": "排除原因", "status": "available" if original_channel else "missing", "evidence": {"reason": None if original_channel else "authoritative_channel_missing"}},
                {"step": 5, "title": "基线评分", "status": "available" if row.get("decision_id") else "missing", "evidence": {"decision_id": row.get("decision_id"), "configuration_version": row.get("configuration_version")}},
                {"step": 6, "title": "原始选择", "status": "available", "evidence": {"model": model or None, "channel_id": original_channel}},
                {"step": 7, "title": "真实执行结果", "status": "available", "evidence": actual},
                {"step": 8, "title": "候选策略评分", "status": "estimated" if candidate_key else "unavailable", "evidence": {"strategy": candidate_strategy, "candidate_count": len(candidates), "selection_reason": reason}},
                {"step": 9, "title": "候选选择", "status": "estimated" if candidate_key else "unavailable", "evidence": {"model": candidate_key[0] if candidate_key else None, "channel_id": candidate_channel, "confidence": counterfactual["confidence"]}},
                {"step": 10, "title": "差异解释", "status": "estimated" if candidate_key else "unavailable", "evidence": {"choice_changed": counterfactual["choice_changed"], "reason": reason}},
                {"step": 11, "title": "成本计算依据", "status": "estimated" if estimated_cost is not None else "unavailable", "evidence": {"estimated_cost": estimated_cost, "price_version": counterfactual["price_version"], "basis": counterfactual["cost_basis"], "unavailable_reason": cost_reason}},
                {"step": 12, "title": "延迟估算依据", "status": "estimated" if counterfactual["estimated_latency_ms"] is not None else "unavailable", "evidence": {"estimated_latency_ms": counterfactual["estimated_latency_ms"], "sample_count": counterfactual["latency_sample_count"], "basis": counterfactual["latency_basis"], "unavailable_reason": reason}},
            ]
            items.append({"record_id": row["record_id"], "occurred_at": row["occurred_at"],
                          "request_id": row.get("request_id"), "response_id": row.get("response_id"),
                          "decision_id": row.get("decision_id"), "source_type": row.get("source_type"),
                          "association": association, "configuration_version": row.get("configuration_version"),
                          "metric_snapshot_id": row.get("metric_snapshot_id"), "actual": actual,
                          "counterfactual": counterfactual, "timeline": timeline})
        result = self._summarize(replay_id, baseline_strategy, candidate_strategy, source, items)
        created = _now()
        with self.connect() as db:
            db.execute("""INSERT INTO historical_replay_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (replay_id, self.schema_version, filters["environment_id"], baseline_strategy,
                        candidate_strategy, _json(filters), source["source_watermark"], len(items),
                        sum(item["association"] == "exact" for item in items),
                        sum(item["association"] != "exact" for item in items), _json(result), created,
                        *self.scope.sql_parameters()))
            db.executemany("""INSERT INTO historical_replay_items(
                replay_id,record_id,occurred_at,request_id,response_id,decision_id,
                actual_json,counterfactual_json,timeline_json,tenant_id,workspace_id,
                source_type,association,configuration_version,metric_snapshot_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [
                (replay_id, item["record_id"], item["occurred_at"], item["request_id"], item["response_id"],
                 item["decision_id"], _json(item["actual"]), _json(item["counterfactual"]),
                 _json(item["timeline"]), *self.scope.sql_parameters(), item["source_type"],
                 item["association"], item["configuration_version"], item["metric_snapshot_id"])
                for item in items])
        return {**result, "items": items, "created_at": created, "network_calls": 0}

    def _summarize(self, replay_id: str, baseline: str, candidate: str,
                   source: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
        comparable = [item for item in items if item["counterfactual"]["choice_changed"] is not None]
        changed = [item for item in comparable if item["counterfactual"]["choice_changed"]]
        actual_costs = [_decimal(item["actual"]["cost_amount"]) for item in items]
        estimated_costs = [_decimal(item["counterfactual"]["estimated_cost"]) for item in items]
        observed_actual_costs = [item for item in actual_costs if item is not None]
        cost_pairs = [(actual, estimate) for actual, estimate in zip(actual_costs, estimated_costs) if actual is not None and estimate is not None]
        actual_latency = [float(item["actual"]["latency_ms"]) for item in items if item["actual"]["latency_ms"] is not None]
        estimated_latency = [float(item["counterfactual"]["estimated_latency_ms"]) for item in items if item["counterfactual"]["estimated_latency_ms"] is not None]
        actual_channels = [item["actual"]["channel_id"] for item in items if item["actual"]["channel_id"]]
        candidate_channels = [item["counterfactual"]["channel_id"] for item in items if item["counterfactual"]["channel_id"]]
        def hhi(values: list[str]) -> float | None:
            if not values:
                return None
            counts = Counter(values)
            return round(sum((count / len(values)) ** 2 for count in counts.values()), 6)
        # Historical actual spend is an observed fact from the CSV `花费`
        # column.  It must not depend on whether a candidate price/version can
        # be matched for a counterfactual estimate.
        actual_cost = sum(observed_actual_costs, Decimal("0")) if observed_actual_costs else None
        candidate_cost = sum(pair[1] for pair in cost_pairs) if cost_pairs else None
        cost_delta_pct = None if actual_cost in (None, Decimal(0)) or candidate_cost is None else round(float((candidate_cost - actual_cost) / actual_cost * 100), 3)
        latency_coverage = len(estimated_latency)
        timeout_count = sum(
            str(item["actual"].get("error_category") or "").lower() == "timeout"
            or item["actual"].get("http_status") in {408, 504, 524}
            for item in items
        )
        rate_limit_count = sum(
            str(item["actual"].get("error_category") or "").lower() in {"rate_limit", "rate_limited"}
            or item["actual"].get("http_status") == 429
            for item in items
        )
        actual_failure_count = sum(item["actual"].get("status") != "SUCCESS" for item in items)
        risk_count = sum(
            item["counterfactual"]["confidence"] == "low"
            or item["counterfactual"]["channel_id"] is None
            for item in items
        )
        actual_input = sum(int(item["actual"].get("input_tokens") or 0) for item in items)
        actual_output = sum(int(item["actual"].get("output_tokens") or 0) for item in items)
        actual_models = {str(item["actual"].get("model")) for item in items if item["actual"].get("model")}
        exact_count = sum(item["association"] == "exact" for item in items)
        historical_statistics_count = sum(item["association"] == "historical_statistics" for item in items)
        execution_only_count = sum(item["association"] == "execution_only" for item in items)
        historical_request_content_missing = sum(
            item["counterfactual"].get("reason") == "historical_request_content_missing"
            for item in items
        )
        conclusion = (f"{len(items)}条真实日志的实际Token、费用和延迟统计可用；"
                      f"候选策略可判断{len(comparable)}条选择，改变{len(changed)}次；"
                      + (f"可比样本候选成本变化{cost_delta_pct:+.1f}%；" if cost_delta_pct is not None else "候选策略成本暂不可计算；")
                      + (f"{latency_coverage}条具备候选延迟样本；" if latency_coverage else "候选渠道样本不足，暂不能证明整体延迟改善；")
                      + (f"{historical_request_content_missing}条历史CSV缺少请求内容，不能进行请求级模型重新选择。" if historical_request_content_missing else f"{risk_count}条存在低置信度或不可估算风险。"))
        return {"replay_id": replay_id, "schema_version": self.schema_version,
                "execution_mode": "offline_counterfactual_no_provider_call", "is_mock": False,
                "baseline_strategy": baseline, "candidate_strategy": candidate,
                "source": source, "sample_count": len(items),
                "exact_association_count": exact_count,
                "unlinked_count": len(items) - exact_count,
                "historical_statistics_count": historical_statistics_count,
                "execution_only_count": execution_only_count,
                "choice_comparable_count": len(comparable), "choice_changed_count": len(changed),
                "choice_changed_rate": round(len(changed) / len(comparable), 6) if comparable else None,
                "actual": {
                    "record_count": len(items), "input_tokens": actual_input,
                    "cached_input_tokens": sum(int(item["actual"].get("cached_input_tokens") or 0) for item in items),
                    "output_tokens": actual_output, "total_tokens": actual_input + actual_output,
                    "total_cost": format(actual_cost, "f") if actual_cost is not None else None,
                    "average_cost": format(actual_cost / len(observed_actual_costs), "f") if actual_cost is not None else None,
                    "cost_coverage_count": len(observed_actual_costs),
                    "p50_latency_ms": _percentile(actual_latency, .50),
                    "p95_latency_ms": _percentile(actual_latency, .95),
                    "p99_latency_ms": _percentile(actual_latency, .99),
                    "failure_count": actual_failure_count,
                    "stream_count": sum(bool(item["actual"].get("stream")) for item in items),
                    "nonstream_count": sum(not bool(item["actual"].get("stream")) for item in items),
                    "model_count": len(actual_models), "currency": "CNY" if actual_cost is not None else None,
                    "price_source": "historical_log_detail" if historical_statistics_count else None,
                    "price_version": "unversioned_historical_snapshot" if historical_statistics_count else None,
                },
                "cost": {"baseline": format(actual_cost, "f") if actual_cost is not None else None,
                         "candidate": format(candidate_cost, "f") if candidate_cost is not None else None,
                         "delta_percent": cost_delta_pct, "coverage_count": len(cost_pairs),
                         "actual_coverage_count": len(observed_actual_costs),
                         "candidate_unavailable_reason": None if candidate_cost is not None else (
                             "historical_request_content_missing" if historical_request_content_missing
                             else "candidate_model_or_channel_price_missing"),
                         "actual_formula": "统一日志中已发生调用的实际花费求和",
                         "formula": "历史输入/输出Token × 候选模型渠道价格版本",
                         "currency": "CNY" if actual_cost is not None or candidate_cost is not None else None},
                "latency": {"baseline_p95_ms": _percentile(actual_latency, .95),
                            "candidate_p95_ms": _percentile(estimated_latency, .95),
                            "coverage_count": latency_coverage,
                            "formula": "相同模型与渠道历史样本P95（最少3条）"},
                "success_changes": {"success_to_failure": None, "failure_to_success": None,
                                    "reason": "离线回放不调用Provider，不能推断真实成功状态变化"},
                "sla": {"baseline_failure_count": actual_failure_count,
                          "baseline_timeout_count": timeout_count,
                          "baseline_rate_limit_count": rate_limit_count,
                          "baseline_p95_ms": _percentile(actual_latency, .95),
                          "baseline_p99_ms": _percentile(actual_latency, .99),
                          "candidate_risk": None, "coverage_count": len(items),
                          "reason": "候选策略未调用Provider，无法判断候选SLA风险"},
                "fallback": {"baseline": sum(item["actual"]["fallback"] for item in items),
                             "candidate": None, "reason": "反事实执行未发生"},
                "concentration": {"baseline_hhi": hhi(actual_channels), "candidate_hhi": hhi(candidate_channels),
                                  "formula": "HHI = Σ(渠道选择占比²)",
                                  "reason": None if candidate_channels else "渠道证据缺失，无法计算"},
                "low_confidence_count": sum(item["counterfactual"]["confidence"] == "low" for item in items),
                "unestimable_count": sum(item["counterfactual"]["channel_id"] is None for item in items),
                "risk_count": risk_count,
                "conclusion": conclusion, "disclaimer": DISCLAIMER}

    def get(self, replay_id: str) -> dict[str, Any]:
        with self.connect() as db:
            run = db.execute("""SELECT result_json,created_at FROM historical_replay_runs
              WHERE tenant_id=? AND workspace_id=? AND replay_id=?""",
              (*self.scope.sql_parameters(), replay_id)).fetchone()
            if run is None:
                raise HistoricalReplayError("replay_not_found")
            rows = db.execute("""SELECT * FROM historical_replay_items WHERE tenant_id=?
              AND workspace_id=? AND replay_id=? ORDER BY occurred_at DESC,record_id""",
              (*self.scope.sql_parameters(), replay_id)).fetchall()
        result = json.loads(run["result_json"])
        result["created_at"] = run["created_at"]
        result["items"] = [{"record_id": row["record_id"], "occurred_at": row["occurred_at"],
                            "request_id": row["request_id"], "response_id": row["response_id"],
                            "decision_id": row["decision_id"], "source_type": row["source_type"],
                            "association": row["association"] or "unlinked",
                            "configuration_version": row["configuration_version"],
                            "metric_snapshot_id": row["metric_snapshot_id"],
                            "actual": json.loads(row["actual_json"]),
                            "counterfactual": json.loads(row["counterfactual_json"]),
                            "timeline": json.loads(row["timeline_json"])} for row in rows]
        return result

    def export_csv(self, replay_id: str) -> str:
        result = self.get(replay_id)
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=["occurred_at", "request_id", "response_id", "decision_id",
            "actual_model", "actual_channel", "actual_status", "actual_cost", "candidate_model",
            "candidate_channel", "choice_changed", "estimated_cost", "cost_delta", "estimated_latency_ms",
            "confidence", "reason"])
        writer.writeheader()
        for item in result["items"]:
            actual, estimate = item["actual"], item["counterfactual"]
            writer.writerow({"occurred_at": item["occurred_at"], "request_id": item["request_id"] or "",
                "response_id": item["response_id"] or "", "decision_id": item["decision_id"] or "",
                "actual_model": actual["model"] or "", "actual_channel": actual["channel_id"] or "",
                "actual_status": actual["status"] or "", "actual_cost": actual["cost_amount"] or "",
                "candidate_model": estimate["model"] or "", "candidate_channel": estimate["channel_id"] or "",
                "choice_changed": "" if estimate["choice_changed"] is None else str(estimate["choice_changed"]).lower(),
                "estimated_cost": estimate["estimated_cost"] or "", "cost_delta": estimate["cost_delta"] or "",
                "estimated_latency_ms": estimate["estimated_latency_ms"] if estimate["estimated_latency_ms"] is not None else "",
                "confidence": estimate["confidence"], "reason": estimate["reason"] or estimate["cost_unavailable_reason"] or ""})
        return output.getvalue()
