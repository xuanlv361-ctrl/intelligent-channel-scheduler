"""Atomic local JSON storage for agent memory records."""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STORE = ROOT / "data" / "agent_memory_v1.json"
MEMORY_TYPES = {"conversation", "user_preference", "task_history"}
REQUIRED_FIELDS = {
    "memory_id", "user_id", "memory_type", "content", "timestamp", "importance"
}


def _empty_store() -> dict[str, Any]:
    return {
        "memory_version": "agent-memory-v1.0.0",
        "source_type": "offline_local_json",
        "records": [],
    }


def _load(path: Path) -> dict[str, Any]:
    if not Path(path).exists():
        return _empty_store()
    with Path(path).open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def _validate(record: Mapping[str, Any]) -> None:
    if set(record) != REQUIRED_FIELDS:
        raise ValueError("memory record fields do not match schema")
    if not all(
        isinstance(record[key], str) and record[key]
        for key in ("memory_id", "user_id", "memory_type", "timestamp")
    ):
        raise ValueError("memory identifiers and timestamp must be non-empty strings")
    if record["memory_type"] not in MEMORY_TYPES:
        raise ValueError("unsupported memory_type")
    if not isinstance(record["content"], (str, dict, list)):
        raise ValueError("memory content must be text, object, or list")
    if isinstance(record["importance"], bool) or not isinstance(
        record["importance"], (int, float)
    ) or not 0 <= float(record["importance"]) <= 1:
        raise ValueError("importance must be numeric in [0,1]")
    datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
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


def save_memory(
    memory: Mapping[str, Any], *, path: Path = DEFAULT_STORE
) -> dict[str, Any]:
    _validate(memory)
    store = _load(path)
    if any(row["memory_id"] == memory["memory_id"] for row in store["records"]):
        raise ValueError("memory_id already exists")
    store["records"].append(deepcopy(dict(memory)))
    store["records"].sort(key=lambda row: (row["timestamp"], row["memory_id"]))
    _atomic_write(Path(path), store)
    return deepcopy(dict(memory))


def query_memory(
    *,
    path: Path = DEFAULT_STORE,
    user_id: str | None = None,
    memory_type: str | None = None,
) -> list[dict[str, Any]]:
    rows = _load(path)["records"]
    return [
        deepcopy(row) for row in rows
        if (user_id is None or row["user_id"] == user_id)
        and (memory_type is None or row["memory_type"] == memory_type)
    ]


def delete_memory(memory_id: str, *, path: Path = DEFAULT_STORE) -> bool:
    store = _load(path)
    remaining = [
        row for row in store["records"] if row["memory_id"] != memory_id
    ]
    if len(remaining) == len(store["records"]):
        return False
    store["records"] = remaining
    _atomic_write(Path(path), store)
    return True
