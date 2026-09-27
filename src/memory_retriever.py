"""Deterministic retrieval of preferences and task history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from memory_store import DEFAULT_STORE, query_memory


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return " ".join(f"{key} {value}" for key, value in sorted(content.items()))
    return " ".join(map(str, content))


def retrieve_context(
    user_id: str,
    task: str,
    *,
    path: Path = DEFAULT_STORE,
    limit: int = 5,
) -> dict[str, Any]:
    if not user_id or not task or limit <= 0:
        raise ValueError("user_id, task, and positive limit are required")
    rows = query_memory(path=path, user_id=user_id)
    ranked = sorted(
        rows,
        key=lambda row: (
            -float(row["importance"]), row["timestamp"], row["memory_id"]
        ),
    )
    preferences = [
        _content_text(row["content"])
        for row in ranked if row["memory_type"] == "user_preference"
    ][:limit]
    history = []
    for row in ranked:
        if row["memory_type"] != "task_history":
            continue
        content = row["content"]
        value = content.get("task_type") if isinstance(content, dict) else content
        history.append(str(value))
    task_terms = {term.casefold() for term in task.split() if len(term) > 2}
    conversations = [
        _content_text(row["content"]) for row in ranked
        if row["memory_type"] == "conversation"
        and (
            not task_terms
            or any(term in _content_text(row["content"]).casefold()
                   for term in task_terms)
        )
    ][:limit]
    return {
        "relevant_preferences": preferences,
        "previous_tasks": history[:limit],
        "recent_context": conversations,
        "memory_count_considered": len(rows),
        "source_type": "offline_deterministic_memory_retrieval",
    }
