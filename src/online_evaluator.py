"""Rule-based offline response evaluator."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_PATH = ROOT / "output" / "model_evaluation_results_v1.json"
SUPPORTED_TASKS = {"coding", "writing", "reasoning", "qa"}


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 3)


def evaluate_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate completeness, rule-based correctness proxies, and format."""
    task_type = str(payload.get("task_type", "")).lower()
    prompt = str(payload.get("prompt", "")).strip()
    response = str(payload.get("response", "")).strip()
    if task_type not in SUPPORTED_TASKS:
        raise ValueError(f"unsupported task_type: {task_type}")
    if not prompt or not response:
        raise ValueError("prompt and response must be non-empty")

    issues: list[str] = []
    words = re.findall(r"\b[\w'-]+\b", response)
    length_score = min(1.0, len(words) / 40)
    completeness = 0.35 + 0.65 * length_score
    correctness = 0.6
    format_score = 0.7

    if task_type == "coding":
        has_code_block = "```" in response
        has_syntax = bool(re.search(
            r"\b(def|class|return|import|function|const|let|SELECT)\b", response
        ))
        correctness = 0.35 + 0.35 * has_syntax + 0.3 * (
            "test" in response.casefold() or "assert" in response
        )
        format_score = 1.0 if has_code_block else 0.45
        if not has_code_block:
            issues.append("missing code block")
        if not has_syntax:
            issues.append("missing recognizable syntax")
        if "test" not in response.casefold() and "assert" not in response:
            issues.append("missing verification example")
    elif task_type == "writing":
        structured = bool(re.search(r"(^|\n)(#{1,3}\s|\d+\.\s|[-*]\s)", response))
        format_score = 1.0 if structured else 0.55
        correctness = 0.75
        if len(words) < 60:
            issues.append("response is shorter than the writing heuristic")
        if not structured:
            issues.append("missing visible structure")
    elif task_type == "reasoning":
        markers = sum(marker in response.casefold() for marker in (
            "because", "therefore", "however", "first", "second",
            "因为", "因此", "但是", "首先", "其次",
        ))
        correctness = min(1.0, 0.45 + 0.15 * markers)
        format_score = 0.9 if markers >= 2 else 0.65
        if markers < 2:
            issues.append("reasoning chain is not sufficiently explicit")
        if len(words) < 35:
            issues.append("analysis appears incomplete")
    else:
        prompt_terms = {
            term for term in re.findall(r"\b[a-zA-Z]{4,}\b", prompt.casefold())
            if term not in {"what", "when", "where", "which", "that", "this"}
        }
        overlap = sum(term in response.casefold() for term in prompt_terms)
        correctness = min(1.0, 0.55 + 0.1 * overlap)
        format_score = 0.9
        if len(words) < 12:
            issues.append("answer appears incomplete")

    completeness = _clamp(completeness)
    correctness = _clamp(correctness)
    format_score = _clamp(format_score)
    score = _clamp(
        0.4 * completeness + 0.4 * correctness + 0.2 * format_score
    )
    return {
        "evaluation_score": score,
        "metrics": {
            "completeness": completeness,
            "correctness": correctness,
            "format": format_score,
        },
        "issues": issues,
        "evaluation_method": "offline_rule_based_v1",
    }


def build_demo() -> dict[str, Any]:
    scenarios = [
        {
            "model": "deepseek-reasoner", "task_type": "coding",
            "prompt": "Write a Python add function with a test.",
            "response": (
                "```python\ndef add(a, b):\n    return a + b\n\n"
                "assert add(2, 3) == 5\n```\nThe assertion is a small test."
            ),
        },
        {
            "model": "qwen", "task_type": "writing",
            "prompt": "Write a structured introduction to routing.",
            "response": "Routing sends work to a suitable destination.",
        },
        {
            "model": "claude-sonnet", "task_type": "reasoning",
            "prompt": "Compare cost and quality trade-offs.",
            "response": (
                "First, quality may require a stronger model because difficult "
                "tasks need more capability. Second, cost favors smaller models. "
                "However, routing by task complexity balances both objectives; "
                "therefore the preferred choice depends on requirements."
            ),
        },
        {
            "model": "gemini-flash", "task_type": "qa",
            "prompt": "What is model routing?",
            "response": (
                "Model routing is the process of selecting an appropriate model "
                "for a request based on its requirements and configured policy."
            ),
        },
    ]
    return {
        "source_type": "synthetic_offline_evaluation_fixture",
        "real_api_calls_performed": 0,
        "records": [
            {
                "evaluation_id": f"EVAL-{index:03d}",
                **scenario,
                **evaluate_response(scenario),
            }
            for index, scenario in enumerate(scenarios, 1)
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    payload = build_demo()
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "status": "ok", "record_count": len(payload["records"]),
        "dry_run": args.dry_run, "real_api_calls_performed": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
