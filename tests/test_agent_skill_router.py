import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_skill_router import select_tools


def ids(result):
    return [row["tool_id"] for row in result["selected_tools"]]


def test_coding_agent_selects_explainable_tools():
    result = select_tools(
        "coding_agent", "Analyze source code and run Python tests."
    )
    assert ids(result) == ["code_analyzer_tool", "python_executor_tool"]
    assert all(row["reason"] for row in result["selected_tools"])


def test_role_preferences_and_task_keywords_are_applied():
    assert ids(select_tools("research_agent", "Research an uploaded file.")) == [
        "search_mock_tool", "file_analyzer_tool"
    ]
    assert ids(select_tools("reasoning_agent", "Calculate this math equation.")) == [
        "calculator_tool"
    ]
    assert ids(select_tools("writer_agent", "Summarize this document.")) == [
        "file_analyzer_tool"
    ]


def test_selection_is_deterministic_and_catalog_is_offline():
    first = select_tools("coding_agent", "test code")
    assert first == select_tools("coding_agent", "test code")
    catalog = json.loads(
        (ROOT / "data" / "tool_catalog_v1.json").read_text(encoding="utf-8")
    )
    assert len(catalog["tools"]) == 5
    assert all("offline" in row["execution_mode"] or "simulation" in row["execution_mode"]
               for row in catalog["tools"])
