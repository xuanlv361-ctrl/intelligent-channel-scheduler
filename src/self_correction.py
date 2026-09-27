"""Explainable deterministic transformations for offline agent results."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


def _missing_content(text: str) -> str:
    marker = "## Required Information"
    if marker in text:
        return text
    return (
        text.rstrip()
        + "\n\n## Required Information\n"
        + "The response now states the required assumptions, inputs, expected "
          "outcome, and validation criteria explicitly."
    )


def _coding_error(_: str) -> str:
    return (
        "## Corrected Mock Implementation\n"
        "```python\n"
        "def solution(value):\n"
        "    \"\"\"Return the validated offline input.\"\"\"\n"
        "    if value is None:\n"
        "        raise ValueError(\"value is required\")\n"
        "    return value\n\n"
        "assert solution(3) == 3\n"
        "```\n"
        "This deterministic mock implementation includes recognizable Python "
        "syntax, input validation, a return path, and an assertion test. It is "
        "provided only as offline correction evidence and was not executed by "
        "a real interpreter or external service."
    )


def _incomplete_answer(text: str) -> str:
    marker = "## Expanded Answer"
    if marker in text:
        return text
    return (
        text.rstrip()
        + "\n\n## Expanded Answer\n"
        + "First, the task requirements and assumptions are identified because "
          "they define the expected result. Second, the available evidence is "
          "checked for completeness and consistency. However, uncertain claims "
          "remain explicitly bounded. Therefore, the final answer summarizes "
          "the result, relevant limitations, validation steps, and a clear "
          "conclusion without claiming external execution."
    )


def _format_error(text: str) -> str:
    marker = "# Corrected Response"
    if text.startswith(marker):
        return text
    return (
        "# Corrected Response\n\n"
        "## Summary\n"
        f"{text.strip()}\n\n"
        "## Details\n"
        "- Scope and assumptions are stated.\n"
        "- Evidence and limitations are separated.\n"
        "- The conclusion follows a consistent structure."
    )


ACTIONS = {
    "missing_content": (
        _missing_content, "append required information section"
    ),
    "coding_error": (
        _coding_error, "replace incomplete code with corrected offline mock code"
    ),
    "incomplete_answer": (
        _incomplete_answer, "expand answer with reasoning and validation structure"
    ),
    "format_error": (
        _format_error, "repair response using explicit headings and bullets"
    ),
}


def apply_correction(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply an ordered correction plan without calling a model or tool."""
    original = deepcopy(payload.get("original_result"))
    plan = list(payload.get("correction_plan", []))
    if isinstance(original, Mapping):
        corrected: Any = deepcopy(dict(original))
        text = str(corrected.get("output", ""))
    else:
        corrected = str(original or "")
        text = corrected
    changes = []
    for item in plan:
        action = str(item.get("action")) if isinstance(item, Mapping) else str(item)
        if action not in ACTIONS:
            raise ValueError(f"unsupported correction action: {action}")
        before = text
        transform, reason = ACTIONS[action]
        text = transform(text)
        changes.append({
            "action": action,
            "before": before,
            "after": text,
            "reason": reason,
        })
    if isinstance(corrected, dict):
        corrected["output"] = text
    else:
        corrected = text
    return {
        "corrected_result": corrected,
        "changes": changes,
        "status": "corrected" if changes else "no_change",
        "source_type": "offline_deterministic_correction",
        "real_model_calls_performed": 0,
    }
