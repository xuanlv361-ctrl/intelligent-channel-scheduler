import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from memory_retriever import retrieve_context
from memory_store import delete_memory, query_memory, save_memory


def record(memory_id, memory_type, content, importance=0.5):
    return {
        "memory_id": memory_id,
        "user_id": "USER-1",
        "memory_type": memory_type,
        "content": content,
        "timestamp": "2026-07-24T12:00:00+08:00",
        "importance": importance,
    }


def test_store_query_and_delete_memory(tmp_path):
    path = tmp_path / "memory.json"
    save_memory(record("M-1", "conversation", "Python routing task"), path=path)
    assert query_memory(path=path, user_id="USER-1")[0]["memory_id"] == "M-1"
    assert delete_memory("M-1", path=path) is True
    assert query_memory(path=path) == []
    assert delete_memory("missing", path=path) is False


def test_empty_memory_handling(tmp_path):
    result = retrieve_context("USER-1", "coding", path=tmp_path / "missing.json")
    assert result["relevant_preferences"] == []
    assert result["previous_tasks"] == []
    assert result["recent_context"] == []


def test_deterministic_retrieval(tmp_path):
    path = tmp_path / "memory.json"
    save_memory(
        record("M-1", "user_preference", "prefers detailed explanations", 0.9),
        path=path,
    )
    save_memory(
        record("M-2", "task_history", {"task_type": "coding"}, 0.8),
        path=path,
    )
    save_memory(
        record("M-3", "conversation", "Prior Python coding context", 0.7),
        path=path,
    )
    first = retrieve_context("USER-1", "Python coding", path=path)
    assert first == retrieve_context("USER-1", "Python coding", path=path)
    assert first["relevant_preferences"] == ["prefers detailed explanations"]
    assert first["previous_tasks"] == ["coding"]
