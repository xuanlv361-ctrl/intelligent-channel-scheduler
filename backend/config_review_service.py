"""Configuration review backed by deduplicated real UAT evidence and immutable versions."""
from __future__ import annotations

import hashlib
import json
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
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
    try:
        result = Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None
    return result if result is not None and result.is_finite() and result >= 0 else None


class ConfigReviewService:
    schema_version = "real_config_review_v2"

    def __init__(self, database: str | Path, config_dir: str | Path,
                 scope: TenantScope | None = None):
        self.database = Path(database)
        self.config_dir = Path(config_dir)
        self.scope = scope or TenantScope.local_development()
        self._init_schema()
        self.ensure_baseline()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _init_schema(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS configuration_versions(
              configuration_version TEXT NOT NULL, revision INTEGER NOT NULL,
              policy_version TEXT, retry_policy_version TEXT,
              payload_json TEXT NOT NULL, diff_json TEXT NOT NULL,
              source_type TEXT NOT NULL, source_sha256 TEXT NOT NULL,
              created_at TEXT NOT NULL, created_by TEXT NOT NULL,
              change_reason TEXT NOT NULL, previous_version TEXT,
              checksum TEXT NOT NULL, is_active INTEGER NOT NULL,
              tenant_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
              PRIMARY KEY(tenant_id,workspace_id,configuration_version));
            CREATE UNIQUE INDEX IF NOT EXISTS uq_configuration_active
              ON configuration_versions(tenant_id,workspace_id) WHERE is_active=1;
            """)

    @staticmethod
    def _table(db: sqlite3.Connection, name: str) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _current_files(self) -> dict[str, Any]:
        decision_path = self.config_dir / "decision_policy_v2.json"
        retry_path = self.config_dir / "retry_policy_v2.json"
        decision = json.loads(decision_path.read_text("utf-8"))
        retry = json.loads(retry_path.read_text("utf-8"))
        strategies = [{"strategy_id": key, "strategy_version": item.get("strategy_version"),
                       "weights": item.get("weights") or {}, "purpose": item.get("purpose")}
                      for key, item in decision.get("strategies", {}).items()]
        payload = {
            "configuration_version": str(decision.get("policy_version") or "decision-policy-v2.0.0"),
            "policy_version": decision.get("policy_version"),
            "retry_policy_version": retry.get("policy_version"),
            "routing_weights": {item["strategy_id"]: item["weights"] for item in strategies},
            "strategies": strategies,
            "timeout": retry.get("per_attempt_timeout_seconds"),
            "retry_limit": retry.get("max_attempts"),
            "fallback_rules": {"retry_same_channel": retry.get("retry_same_channel"),
                               "retryable_errors": retry.get("retryable_errors") or []},
            "circuit_breaker_rules": {}, "allowed_models": [], "allowed_channels": [],
        }
        source = decision_path.read_bytes() + b"\n" + retry_path.read_bytes()
        payload["source_sha256"] = hashlib.sha256(source).hexdigest().upper()
        return payload

    def ensure_baseline(self) -> dict[str, Any]:
        current = self._current_files()
        with self.connect() as db:
            active = db.execute("""SELECT * FROM configuration_versions WHERE tenant_id=?
              AND workspace_id=? AND is_active=1""", self.scope.sql_parameters()).fetchone()
            if active:
                return self._version_row(active)
            version = str(current["configuration_version"])
            checksum = hashlib.sha256(_canonical(current).encode()).hexdigest().upper()
            now = _now()
            db.execute("""INSERT INTO configuration_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                version, 1, current.get("policy_version"), current.get("retry_policy_version"),
                _canonical(current), "{}", "current_persisted_configuration",
                current["source_sha256"], now, "system_baseline", "建立真实配置基线快照",
                None, checksum, 1, *self.scope.sql_parameters()))
            row = db.execute("""SELECT * FROM configuration_versions WHERE tenant_id=?
              AND workspace_id=? AND configuration_version=?""",
              (*self.scope.sql_parameters(), version)).fetchone()
            return self._version_row(row)

    @staticmethod
    def _version_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        item["diff"] = json.loads(item.pop("diff_json"))
        item["is_active"] = bool(item["is_active"])
        return item

    def versions(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""SELECT * FROM configuration_versions WHERE tenant_id=?
              AND workspace_id=? ORDER BY revision DESC""", self.scope.sql_parameters()).fetchall()
        return [self._version_row(row) for row in rows]

    def create_version(self, *, base_version: str, patch: dict[str, Any],
                       created_by: str, change_reason: str) -> dict[str, Any]:
        allowed = {"timeout", "retry_limit", "fallback_rules", "circuit_breaker_rules",
                   "routing_weights", "allowed_models", "allowed_channels"}
        if not patch or set(patch) - allowed:
            raise ValueError("configuration_patch_invalid")
        if not change_reason.strip():
            raise ValueError("configuration_change_reason_required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            base = db.execute("""SELECT * FROM configuration_versions WHERE tenant_id=?
              AND workspace_id=? AND configuration_version=?""",
              (*self.scope.sql_parameters(), base_version)).fetchone()
            active = db.execute("""SELECT * FROM configuration_versions WHERE tenant_id=?
              AND workspace_id=? AND is_active=1""", self.scope.sql_parameters()).fetchone()
            if not base or not active or active["configuration_version"] != base_version:
                raise ValueError("configuration_optimistic_lock_conflict")
            payload = json.loads(base["payload_json"])
            diff: dict[str, Any] = {}
            for key, value in patch.items():
                if payload.get(key) != value:
                    diff[key] = {"before": payload.get(key), "after": value}
                    payload[key] = value
            if not diff:
                raise ValueError("configuration_no_effective_change")
            revision = int(base["revision"]) + 1
            version = f"{payload.get('policy_version') or 'configuration'}-r{revision}"
            payload["configuration_version"] = version
            checksum = hashlib.sha256(_canonical(payload).encode()).hexdigest().upper()
            db.execute("UPDATE configuration_versions SET is_active=0 WHERE tenant_id=? AND workspace_id=?",
                       self.scope.sql_parameters())
            db.execute("""INSERT INTO configuration_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                version, revision, payload.get("policy_version"), payload.get("retry_policy_version"),
                _canonical(payload), _canonical(diff), "controlled_local_change", checksum,
                _now(), created_by, change_reason.strip(), base_version, checksum, 1,
                *self.scope.sql_parameters()))
            row = db.execute("""SELECT * FROM configuration_versions WHERE tenant_id=?
              AND workspace_id=? AND configuration_version=?""",
              (*self.scope.sql_parameters(), version)).fetchone()
            return self._version_row(row)

    def rollback(self, *, target_version: str, active_version: str,
                 created_by: str, change_reason: str) -> dict[str, Any]:
        with self.connect() as db:
            target = db.execute("""SELECT payload_json FROM configuration_versions WHERE tenant_id=?
              AND workspace_id=? AND configuration_version=?""",
              (*self.scope.sql_parameters(), target_version)).fetchone()
        if not target:
            raise ValueError("configuration_target_not_found")
        target_payload = json.loads(target["payload_json"])
        allowed = {"timeout", "retry_limit", "fallback_rules", "circuit_breaker_rules",
                   "routing_weights", "allowed_models", "allowed_channels"}
        patch = {key: target_payload.get(key) for key in allowed}
        result = self.create_version(base_version=active_version, patch=patch,
                                     created_by=created_by,
                                     change_reason=change_reason or f"回滚到 {target_version}")
        result["rollback_target"] = target_version
        return result

    @staticmethod
    def _concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
        channels = [str(row["channel_id"]) for row in rows if row.get("channel_id") and
                    row.get("channel_source") in {"scheduler_decision", "provider_log",
                                                   "request_id_exact_match", "manually_confirmed"}]
        if not channels:
            return {"hhi": None, "maximum_share": None, "channel_count": 0,
                    "sample_count": 0, "basis": "缺少权威 channel_id，无法计算渠道集中度。"}
        counts = Counter(channels); total = len(channels); shares = [count / total for count in counts.values()]
        return {"hhi": round(sum(share * share for share in shares), 6),
                "maximum_share": round(max(shares), 6), "channel_count": len(counts),
                "sample_count": total, "basis": "仅使用带权威 channel_source 的去重实时记录计算 HHI。"}

    def review(self, environment_id: str = "china_uat",
               baseline_version: str | None = None,
               comparison_version: str | None = None) -> dict[str, Any]:
        versions = self.versions(); active = next(item for item in versions if item["is_active"])
        selected_base = next((item for item in versions if item["configuration_version"] == baseline_version), versions[-1])
        selected_compare = next((item for item in versions if item["configuration_version"] == comparison_version), active)
        with self.connect() as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(standardized_call_logs)")}
            duplicate_clause = "AND COALESCE(duplicate_status,'canonical')<>'exact_duplicate'" if "duplicate_status" in columns else ""
            rows = [dict(row) for row in db.execute(f"""SELECT * FROM standardized_call_logs
              WHERE tenant_id=? AND workspace_id=? AND environment_id=?
              AND source_type IN (?,?) AND request_status IN (?,?,?,?,?)
              AND COALESCE(traffic_class,'business')<>'probe' {duplicate_clause}
              ORDER BY occurred_at DESC,cursor_id DESC""",
              (*self.scope.sql_parameters(), environment_id, *REAL_SOURCES, *TERMINAL_STATES)).fetchall()]
            raw_count = int(db.execute("""SELECT COUNT(*) FROM standardized_call_logs WHERE tenant_id=?
              AND workspace_id=? AND environment_id=? AND source_type IN (?,?)
              AND request_status IN (?,?,?,?,?)
              AND COALESCE(traffic_class,'business')<>'probe'""",
              (*self.scope.sql_parameters(), environment_id, *REAL_SOURCES, *TERMINAL_STATES)).fetchone()[0])
            exact_duplicates = raw_count - len(rows)
            candidates = int(db.execute("""SELECT COUNT(*) FROM standardized_call_logs WHERE tenant_id=?
              AND workspace_id=? AND environment_id=? AND duplicate_status='duplicate_candidate'""",
              (*self.scope.sql_parameters(), environment_id)).fetchone()[0]) if "duplicate_status" in columns else 0
        source_counts = Counter(str(row["source_type"]) for row in rows)
        latencies = [float(row["total_latency_ms"]) for row in rows if row.get("total_latency_ms") is not None]
        costs = [value for row in rows if (value := _decimal(row.get("cost_amount"))) is not None]
        success_count = sum(row["request_status"] == "SUCCESS" for row in rows)
        concentration = self._concentration([row for row in rows if row["source_type"] == "realtime_execution"])
        channel_covered = concentration["sample_count"]
        sample_count = len(rows)
        model_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows: model_groups[str(row.get("actual_model") or row.get("requested_model"))].append(row)
        model_metrics = [{"model": model, "sample_count": len(items),
                          "success_rate": round(sum(i["request_status"] == "SUCCESS" for i in items) / len(items), 6),
                          "p95_latency_ms": _percentile([float(i["total_latency_ms"]) for i in items if i.get("total_latency_ms") is not None], .95)}
                         for model, items in sorted(model_groups.items())]
        config = active["payload"]
        config_summary = {"policy_version": config.get("policy_version"), "strategies": config.get("strategies") or [],
                          "fallback_configured": bool((config.get("fallback_rules") or {}).get("retry_same_channel") is False and (config.get("retry_limit") or 0) > 1),
                          "timeout_ms": config.get("timeout") * 1000 if isinstance(config.get("timeout"), (int, float)) else None,
                          "maximum_attempts": config.get("retry_limit"), "retry_policy_version": config.get("retry_policy_version"),
                          "retryable_errors": (config.get("fallback_rules") or {}).get("retryable_errors") or []}
        risks = [] if not rows else [
            {"risk": "Fallback 配置", "status": "已配置" if config_summary["fallback_configured"] else "未配置",
             "observed_count": sum(int(row.get("total_attempts") or 1) > 1 for row in rows), "basis": "当前配置及去重真实调用记录。"},
            {"risk": "请求超时", "status": "已配置" if config_summary["timeout_ms"] is not None else "未配置",
             "observed_count": sum(row["request_status"] == "TIMEOUT" or row.get("error_category") == "timeout" for row in rows), "basis": "当前配置及真实超时事件。"},
        ]
        same_selection = selected_base["configuration_version"] == selected_compare["configuration_version"]
        comparison_rows = [row for row in rows if row.get("configuration_version") in
                           {selected_base["configuration_version"], selected_compare["configuration_version"]}]
        versions_with_data = {row.get("configuration_version") for row in comparison_rows}
        impact_available = not same_selection and {selected_base["configuration_version"], selected_compare["configuration_version"]}.issubset(versions_with_data)
        impact = {"status": "available" if impact_available else "unavailable",
                  "current_version": selected_compare["configuration_version"],
                  "previous_version": selected_base["configuration_version"],
                  "current": None, "previous": None, "cost_difference": None,
                  "latency_difference_ms": None, "concentration_difference": None,
                  "basis": ("版本快照已存在，但只有两个版本都有真实调用证据时才计算效果差异。" if not impact_available
                            else "仅比较两个真实配置版本关联的去重调用记录。")}
        selected_diff: dict[str, dict[str, Any]] = {}
        for key in sorted(set(selected_base["payload"]) | set(selected_compare["payload"])):
            before = selected_base["payload"].get(key)
            after = selected_compare["payload"].get(key)
            if before != after:
                selected_diff[key] = {"before": before, "after": after}
        evidence = [{"occurred_at": row["occurred_at"], "request_id": row.get("request_id"),
                     "decision_id": row.get("decision_id"), "model": row.get("actual_model") or row.get("requested_model"),
                     "channel_id": row.get("channel_id"), "channel_source": row.get("channel_source") or "unknown",
                     "status": row["request_status"], "latency_ms": row.get("total_latency_ms"),
                     "cost_amount": row.get("cost_amount"), "currency": row.get("currency"),
                     "configuration_version": row.get("configuration_version")}
                    for row in rows if row.get("request_id") or row.get("decision_id")][:50]
        return {"status": "ready" if rows else "insufficient_data",
                "empty_message": None if rows else "暂无真实日志，无法完成评审。",
                "environment_id": environment_id, "is_mock": False,
                "data_source": {"sources": [{"source": key, "count": value} for key, value in sorted(source_counts.items())],
                                "time_from": min((row["occurred_at"] for row in rows), default=None),
                                "time_to": max((row["occurred_at"] for row in rows), default=None),
                                "sample_count": sample_count, "raw_count": raw_count,
                                "deduplicated_count": sample_count, "exact_duplicate_count": exact_duplicates,
                                "duplicate_candidate_count": candidates,
                                "exact_link_count": sum(bool(row.get("request_id") or row.get("decision_id")) for row in rows),
                                "channel_coverage_count": channel_covered,
                                "channel_coverage_rate": round(channel_covered / sample_count, 6) if sample_count else None,
                                "last_synced_at": max((row["updated_at"] for row in rows), default=None),
                                "configuration_version": active["configuration_version"],
                                "price_version": None, "price_synced_at": None,
                                "metric_snapshot_id": None, "metric_generated_at": None},
                "configuration": config_summary, "versions": versions,
                "selected_versions": {"baseline": selected_base["configuration_version"],
                                      "comparison": selected_compare["configuration_version"]},
                "configuration_diff": selected_diff,
                "metrics": {"sample_count": sample_count,
                            "success_rate": round(success_count / sample_count, 6) if sample_count else None,
                            "p95_latency_ms": _percentile(latencies, .95), "p99_latency_ms": _percentile(latencies, .99),
                            "input_tokens": sum(int(row.get("input_tokens") or 0) for row in rows),
                            "output_tokens": sum(int(row.get("output_tokens") or 0) for row in rows),
                            "total_cost": format(sum(costs, Decimal("0")), "f") if costs else None,
                            "average_cost": format(sum(costs, Decimal("0")) / len(costs), "f") if costs else None,
                            "cost_coverage": round(len(costs) / sample_count, 6) if sample_count else None,
                            "timeout_count": sum(row["request_status"] == "TIMEOUT" for row in rows),
                            "retry_count": sum(max(int(row.get("total_attempts") or 1) - 1, 0) for row in rows),
                            "fallback_count": sum(int(row.get("total_attempts") or 1) > 1 for row in rows),
                            "concentration": concentration, "model_metrics": model_metrics},
                "risks": risks, "impact": impact,
                "rollback": {"status": "available" if len(versions) > 1 else "unavailable",
                             "target_version": versions[-1]["configuration_version"] if len(versions) > 1 else None,
                             "message": ("可选择真实历史快照创建不可变回滚版本。" if len(versions) > 1 else
                                         "当前仅有一个真实配置版本，已建立基线快照；产生下一版本后可进行跨版本比较。")},
                "evidence": evidence,
                "technical": {"schema_version": self.schema_version, "reviewed_at": _now(),
                              "deduplication_boundary": "exact_duplicate excluded; duplicate_candidate retained"}}
