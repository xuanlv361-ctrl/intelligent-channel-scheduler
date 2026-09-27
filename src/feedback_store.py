"""Atomic JSON feedback storage for offline analysis only."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "data" / "user_feedback_schema_v1.json"
DEFAULT_STORE = ROOT / "data" / "user_feedback_v1.json"


class FeedbackValidationError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def validate_feedback(
    feedback: Mapping[str, Any],
    schema: Mapping[str, Any] | None = None,
) -> None:
    schema = schema or load_json(DEFAULT_SCHEMA)
    required = set(schema["required"])
    if set(feedback) != required:
        missing = required - set(feedback)
        extra = set(feedback) - required
        raise FeedbackValidationError(
            f"feedback schema mismatch; missing={sorted(missing)} extra={sorted(extra)}"
        )
    text_fields = (
        "feedback_id", "request_id", "task_type",
        "selected_model", "selected_channel", "timestamp",
    )
    if any(not isinstance(feedback[field], str) or not feedback[field] for field in text_fields):
        raise FeedbackValidationError("required text fields must be non-empty strings")
    if feedback["task_type"] not in schema["properties"]["task_type"]["enum"]:
        raise FeedbackValidationError("unsupported task_type")
    if type(feedback["success"]) is not bool:
        raise FeedbackValidationError("success must be boolean")
    for field in ("latency_ms", "cost", "user_rating"):
        value = feedback[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FeedbackValidationError(f"{field} must be numeric")
    if feedback["latency_ms"] < 0 or feedback["cost"] < 0:
        raise FeedbackValidationError("latency_ms and cost must be non-negative")
    if not 1 <= feedback["user_rating"] <= 5:
        raise FeedbackValidationError("user_rating must be in [1,5]")
    try:
        datetime.fromisoformat(feedback["timestamp"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedbackValidationError("timestamp must be ISO-8601") from exc


def _empty_store() -> dict[str, Any]:
    return {
        "feedback_version": "user-feedback-v1.0.0",
        "source_type": "offline_feedback_log",
        "records": [],
        "limitations": ["Feedback analysis does not update model weights automatically."],
    }


def load_store(path: Path = DEFAULT_STORE) -> dict[str, Any]:
    return load_json(path) if Path(path).exists() else _empty_store()


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def record_feedback(
    feedback: Mapping[str, Any],
    *,
    path: Path = DEFAULT_STORE,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    validate_feedback(feedback, load_json(schema_path))
    store = load_store(path)
    if any(row["feedback_id"] == feedback["feedback_id"] for row in store["records"]):
        raise FeedbackValidationError("feedback_id already exists")
    store["records"].append(deepcopy(dict(feedback)))
    store["records"].sort(key=lambda row: (row["timestamp"], row["feedback_id"]))
    _atomic_write(Path(path), store)
    return deepcopy(dict(feedback))


def query_feedback(
    *,
    path: Path = DEFAULT_STORE,
    model_id: str | None = None,
    task_type: str | None = None,
    success: bool | None = None,
) -> list[dict[str, Any]]:
    rows = load_store(path)["records"]
    return [
        deepcopy(row) for row in rows
        if (model_id is None or row["selected_model"] == model_id)
        and (task_type is None or row["task_type"] == task_type)
        and (success is None or row["success"] is success)
    ]


def aggregate_feedback(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in records:
        groups.setdefault((row["selected_model"], row["task_type"]), []).append(row)
    return [
        {
            "model_id": model,
            "task_type": task,
            "sample_count": len(rows),
            "success_rate": sum(row["success"] for row in rows) / len(rows),
            "average_rating": fmean(float(row["user_rating"]) for row in rows),
            "average_latency": fmean(float(row["latency_ms"]) for row in rows),
            "average_cost": fmean(float(row["cost"]) for row in rows),
        }
        for (model, task), rows in sorted(groups.items())
    ]
