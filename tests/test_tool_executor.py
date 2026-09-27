import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tool_executor import execute_tool


def test_calculator_tool():
    result = execute_tool("calculator_tool", {"expression": "2 + 3 * 4"})
    assert result["status"] == "success"
    assert result["result"]["value"] == 14


def test_code_and_file_analyzers():
    code = execute_tool(
        "code_analyzer_tool", {"source_code": "def f():\n    return 1\n"}
    )
    assert code["result"] == {
        "language": "python", "file_count": 1, "complexity_estimate": "low"
    }
    document = execute_tool(
        "file_analyzer_tool", {"file_content": "one two\nthree"}
    )
    assert document["result"]["word_count"] == 3
    assert document["result"]["line_count"] == 2


def test_python_executor_is_restricted_simulation():
    result = execute_tool(
        "python_executor_tool", {"python_code": "print(1 + 2)"}
    )
    assert result["status"] == "success"
    assert result["result"] == {"stdout": "3\n", "success": True}
    blocked = execute_tool(
        "python_executor_tool", {"python_code": "import os\nprint(os.getcwd())"}
    )
    assert blocked["status"] == "failed"
    assert blocked["result"] is None
    assert blocked["real_tool_called"] is False


def test_search_is_mock_and_deterministic():
    first = execute_tool("search_mock_tool", {"query": "gateway research"})
    assert first == execute_tool("search_mock_tool", {"query": "gateway research"})
    assert first["result"]["results"][0]["result_id"].startswith("MOCK-")
    assert first["real_api_calls_performed"] == 0
