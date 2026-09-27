"""Unified search over already-authorized local evidence.

The service deliberately indexes a fixed allowlist of sanitized fields. It never
loads request/response bodies, prompts, credentials, headers, cookies or storage.
"""
from __future__ import annotations

import base64
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.services.console_service import Store, classify

ALLOWED_ENVIRONMENTS = {"all", "china_uat", "overseas"}
ALLOWED_SOURCE_TYPES = {
    "demo_mock", "offline_estimate", "offline_replay", "measured_uat",
    "measured_unified_uat", "measured_uat_browser_collector",
    "measured_overseas_browser_collector",
}
SEARCH_FIELDS = {
    "request_id", "response_id", "decision_id", "execution_id", "collection_id",
    "error_id", "bug_id", "channel_id", "channel_name", "requested_model",
    "actual_model", "error_category", "error_layer", "strategy", "execution_status",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _updated(row: dict[str, Any]) -> str | None:
    for key in ("updated_at", "created_at", "timestamp", "logged_at", "collected_at"):
        if row.get(key):
            return str(row[key])
    return None


def _cursor_decode(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except Exception as exc:
        raise ValueError("search_cursor_invalid") from exc


def _cursor_encode(value: int) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).decode()


class UnifiedSearchService:
    def __init__(self, database_path: Path, store: Store):
        self.database_path = Path(database_path)
        self.store = store

    def _connect(self):
        db = sqlite3.connect(self.database_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _result(entity_type: str, primary_id: str, title: str, summary: str,
                row: dict[str, Any], matched_fields: list[str],
                destination_path: str) -> dict[str, Any]:
        source = _text(row.get("source_type") or "measured_uat")
        environment = _text(row.get("environment_id") or row.get("environment") or "china_uat")
        return {
            "entity_type": entity_type, "primary_id": primary_id, "title": title,
            "summary": summary, "environment_id": environment, "source_type": source,
            "is_mock": source == "demo_mock",
            "sample_or_evidence_id": _text(
                row.get("collection_id") or row.get("import_batch_id")
                or row.get("batch_id") or primary_id),
            "updated_at": _updated(row), "destination_path": destination_path,
            "matched_fields": matched_fields,
        }

    def _record_results(self, query: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for row in self.store.records():
            row = dict(row)
            searchable = {field: row.get(field) for field in SEARCH_FIELDS if row.get(field) not in (None, "")}
            matched = sorted(field for field, value in searchable.items() if query in _text(value).casefold())
            if not matched:
                continue
            request_id = _text(row.get("request_id") or row.get("response_id") or row.get("raw_record_sha256"))
            if {"request_id", "response_id", "execution_id"} & set(matched):
                results.append(self._result(
                    "request", request_id, f"请求 {request_id}",
                    " · ".join(filter(None, [_text(row.get("requested_model")), _text(row.get("channel_name"))])),
                    row, matched, f"/errors?q={request_id}"))
            if {"channel_id", "channel_name"} & set(matched):
                channel_id = _text(row.get("channel_id") or row.get("channel_name"))
                results.append(self._result(
                    "channel", channel_id, f"渠道 {_text(row.get('channel_name') or channel_id)}",
                    f"渠道 ID {channel_id}", row, matched, "/health"))
            if {"requested_model", "actual_model"} & set(matched):
                model = _text(row.get("actual_model") or row.get("requested_model"))
                results.append(self._result(
                    "model", model, f"模型 {model}", "来自已导入请求证据",
                    row, matched, "/mappings"))
            error_value = row.get("error_message")
            status = row.get("http_status")
            if error_value or status not in (None, "", 200, "200"):
                classified = classify(int(status) if _text(status).isdigit() else None, _text(error_value))
                derived = {**row, **classified}
                error_matched = sorted(
                    field for field in ("error_category", "error_layer", "execution_status")
                    if query in _text(derived.get(field)).casefold())
                if error_matched or "error_id" in matched:
                    error_id = _text(row.get("error_id") or request_id)
                    results.append(self._result(
                        "error", error_id, f"错误 {classified['error_category']}",
                        classified["sanitized_message"], derived, error_matched or matched,
                        f"/errors?q={error_id}"))
        return results

    def _sqlite_results(self, query: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        with self._connect() as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "collector_collections" in tables:
                rows = db.execute("""SELECT collection_id,collected_at,source_type,
                  environment_id,import_batch_id FROM collector_collections""").fetchall()
                for raw in rows:
                    row = dict(raw)
                    matched = [field for field in ("collection_id",)
                               if query in _text(row.get(field)).casefold()]
                    if matched:
                        output.append(self._result(
                            "collection", row["collection_id"], f"采集 {row['collection_id']}",
                            "只读浏览器采集证据", row, matched, "/collector"))
            if "uat_executions" in tables:
                columns = {r[1] for r in db.execute("PRAGMA table_info(uat_executions)")}
                safe = [name for name in ("execution_id", "decision_id", "requested_model",
                                          "actual_model", "status", "created_at", "environment_id")
                        if name in columns]
                for raw in db.execute(f"SELECT {','.join(safe)} FROM uat_executions"):
                    row = dict(raw)
                    row.setdefault("source_type", "measured_uat")
                    searchable = {
                        "execution_id": row.get("execution_id"), "decision_id": row.get("decision_id"),
                        "requested_model": row.get("requested_model"), "actual_model": row.get("actual_model"),
                        "execution_status": row.get("status"),
                    }
                    matched = sorted(k for k, v in searchable.items() if query in _text(v).casefold())
                    if matched:
                        identifier = _text(row.get("execution_id") or row.get("decision_id"))
                        output.append(self._result(
                            "decision" if "decision_id" in matched else "request", identifier,
                            f"UAT 执行 {identifier}", _text(row.get("status")), row, matched,
                            "/uat-execution"))
            if "shadow_decisions" in tables:
                columns = {r[1] for r in db.execute("PRAGMA table_info(shadow_decisions)")}
                safe = [name for name in ("decision_id", "execution_id", "strategy", "created_at",
                                          "environment_id") if name in columns]
                if safe:
                    for raw in db.execute(f"SELECT {','.join(safe)} FROM shadow_decisions"):
                        row = dict(raw); row.setdefault("source_type", "offline_estimate")
                        matched = sorted(k for k in ("decision_id", "execution_id", "strategy")
                                         if query in _text(row.get(k)).casefold())
                        if matched:
                            identifier = _text(row.get("decision_id"))
                            output.append(self._result(
                                "decision", identifier, f"影子决策 {identifier}",
                                _text(row.get("strategy")), row, matched, "/shadow"))
        return output

    def search(self, q: str, environment_id: str = "all", source_type: str | None = None,
               limit: int = 25, cursor: str | None = None) -> dict[str, Any]:
        query = q.strip().casefold()
        if not query:
            raise ValueError("search_query_required")
        if environment_id not in ALLOWED_ENVIRONMENTS:
            raise ValueError("search_environment_invalid")
        if source_type and source_type not in ALLOWED_SOURCE_TYPES:
            raise ValueError("search_source_type_invalid")
        if not 1 <= limit <= 100:
            raise ValueError("search_limit_invalid")
        offset = _cursor_decode(cursor)
        items = self._record_results(query) + self._sqlite_results(query)
        unique: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in items:
            if environment_id != "all" and item["environment_id"] != environment_id:
                continue
            if source_type and item["source_type"] != source_type:
                continue
            unique[(item["entity_type"], item["environment_id"], item["primary_id"])] = item
        ordered = sorted(unique.values(), key=lambda x: (
            x["entity_type"], x["environment_id"], x["title"], x["primary_id"]))
        page = ordered[offset:offset + limit]
        next_cursor = _cursor_encode(offset + limit) if offset + limit < len(ordered) else None
        counts = Counter(item["entity_type"] for item in page)
        sources = sorted({item["source_type"] for item in page})
        environments = sorted({item["environment_id"] for item in page})
        updated = max((_text(item["updated_at"]) for item in page), default="") or None
        return {
            "query": q.strip(), "environment_id": environment_id, "source_type": source_type,
            "items": page, "groups": dict(sorted(counts.items())),
            "next_cursor": next_cursor,
            "provenance": {
                "mode": "local_evidence_search", "environment_id": environment_id,
                "source_type": sources[0] if len(sources) == 1 else "mixed" if sources else None,
                "is_mock": bool(sources) and all(source == "demo_mock" for source in sources),
                "sample_size": len(page), "collection_ids": sorted({
                    item["sample_or_evidence_id"] for item in page
                    if item["entity_type"] == "collection"}),
                "evidence_ids": sorted({item["sample_or_evidence_id"] for item in page}),
                "data_updated_at": updated, "time_range": None, "catalog_version": None,
                "catalog_sha256": None, "policy_version": None, "currency": None,
                "limitations": [
                    "仅搜索已导入并授权的本地结构化证据。",
                    "结果不包含提示词、响应正文、凭证、Cookie 或请求头。",
                ],
                "environments_present": environments,
            },
        }
