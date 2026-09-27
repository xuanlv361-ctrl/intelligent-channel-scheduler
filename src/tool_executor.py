"""Safe deterministic implementations of the offline mock tool catalog."""

from __future__ import annotations

import ast
import hashlib
import re
from typing import Any, Mapping


EXECUTION_TIMES = {
    "calculator_tool": 0.001,
    "code_analyzer_tool": 0.004,
    "python_executor_tool": 0.003,
    "file_analyzer_tool": 0.002,
    "search_mock_tool": 0.005,
}


def _numeric(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _numeric(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left, right = _numeric(node.left), _numeric(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow) and abs(right) <= 10:
            return left ** right
    raise ValueError("expression contains unsupported syntax")


def _calculator(arguments: Mapping[str, Any]) -> dict[str, Any]:
    expression = str(arguments.get("expression", "")).strip()
    if not expression or len(expression) > 200:
        raise ValueError("expression must be a non-empty bounded string")
    value = _numeric(ast.parse(expression, mode="eval").body)
    if abs(float(value)) > 1e100:
        raise ValueError("calculated value exceeds safety bound")
    return {"value": value}


def _code_analyzer(arguments: Mapping[str, Any]) -> dict[str, Any]:
    source = str(arguments.get("source_code", ""))
    if not source:
        raise ValueError("source_code is required")
    try:
        tree = ast.parse(source)
        language = "python"
        branches = sum(isinstance(node, (ast.If, ast.For, ast.While, ast.Try))
                       for node in ast.walk(tree))
        functions = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        for node in ast.walk(tree))
        estimate = "high" if branches + functions >= 8 else (
            "medium" if branches + functions >= 3 else "low"
        )
    except SyntaxError:
        language, estimate = "unknown", "unavailable"
    return {
        "language": language,
        "file_count": 1,
        "complexity_estimate": estimate,
    }


def _python_simulator(arguments: Mapping[str, Any]) -> dict[str, Any]:
    code = str(arguments.get("python_code", ""))
    if not code or len(code) > 10000:
        raise ValueError("python_code must be a non-empty bounded string")
    tree = ast.parse(code, mode="exec")
    output = []
    for statement in tree.body:
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "print"
            and not statement.value.keywords
        ):
            raise ValueError("simulation only supports print(...) expressions")
        values = []
        for argument in statement.value.args:
            if isinstance(argument, ast.Constant) and isinstance(
                argument.value, (str, int, float, bool)
            ):
                values.append(str(argument.value))
            else:
                values.append(str(_numeric(argument)))
        output.append(" ".join(values))
    return {"stdout": "\n".join(output) + ("\n" if output else ""), "success": True}


def _file_analyzer(arguments: Mapping[str, Any]) -> dict[str, Any]:
    content = str(arguments.get("file_content", ""))
    if not content:
        raise ValueError("file_content is required")
    words = re.findall(r"\S+", content)
    return {
        "character_count": len(content),
        "word_count": len(words),
        "line_count": len(content.splitlines()) or 1,
        "summary": content.strip().replace("\n", " ")[:120],
    }


def _search_mock(arguments: Mapping[str, Any]) -> dict[str, Any]:
    query = str(arguments.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
    return {
        "query": query,
        "results": [
            {
                "result_id": f"MOCK-{digest}-1",
                "title": "Synthetic offline result",
                "snippet": f"Offline mock information for: {query}",
            }
        ],
    }


HANDLERS = {
    "calculator_tool": _calculator,
    "code_analyzer_tool": _code_analyzer,
    "python_executor_tool": _python_simulator,
    "file_analyzer_tool": _file_analyzer,
    "search_mock_tool": _search_mock,
}


def execute_tool(tool_id: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Execute a local mock handler and return a stable audit result."""
    if tool_id not in HANDLERS:
        raise ValueError(f"unknown tool_id: {tool_id}")
    try:
        result = HANDLERS[tool_id](arguments)
        status, failure_reason = "success", None
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError) as exc:
        result = None
        status, failure_reason = "failed", str(exc)
    return {
        "tool_id": tool_id,
        "status": status,
        "result": result,
        "execution_time": EXECUTION_TIMES[tool_id],
        "execution_time_type": "fixed_offline_simulation_seconds",
        "failure_reason": failure_reason,
        "real_tool_called": False,
        "real_api_calls_performed": 0,
    }
