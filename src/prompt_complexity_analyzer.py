"""Deterministic, offline prompt complexity analysis."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = ROOT / "config" / "complexity_policy_v1.json"
DEFAULT_OUTPUT_PATH = ROOT / "output" / "prompt_complexity_examples_v1.json"
DIMENSIONS = ("reasoning", "context", "coding", "output", "tool_need")


def _load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        policy = json.load(handle)
    weights = policy.get("aggregate_weights", {})
    if set(weights) != set(DIMENSIONS):
        raise ValueError("complexity policy must define all score dimensions")
    if abs(sum(float(weights[key]) for key in DIMENSIONS) - 1.0) > 1e-9:
        raise ValueError("complexity aggregate weights must sum to 1")
    return policy


def _hits(text: str, terms: tuple[str, ...]) -> int:
    return sum(term in text for term in terms)


def _bounded(base: float, increment: float, hits: int) -> float:
    return round(min(1.0, base + increment * hits), 3)


def analyze_complexity(
    prompt: str, policy: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Estimate five prompt-complexity dimensions using stable keyword rules."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    policy = dict(policy or _load_policy())
    text = prompt.casefold()
    char_count = len(prompt.strip())
    explanations: list[str] = []

    reasoning_hits = _hits(text, (
        "analyze", "analyse", "reason", "prove", "compare", "trade-off",
        "tradeoff", "multi-step", "step by step", "why", "研究", "分析",
        "推理", "论证", "比较", "多步骤",
    ))
    context_hits = _hits(text, (
        "long document", "research", "literature", "sources", "context",
        "全文", "长文档", "研究", "文献", "资料", "上下文",
    ))
    coding_hits = _hits(text, (
        "code", "python", "javascript", "function", "debug", "algorithm",
        "sql", "api", "代码", "编程", "调试", "函数", "算法",
    ))
    output_hits = _hits(text, (
        "report", "essay", "detailed", "comprehensive", "sections",
        "table", "json", "plan", "报告", "文章", "详细", "全面",
        "章节", "表格", "计划",
    ))
    tool_hits = _hits(text, (
        "search", "browse", "database", "file", "spreadsheet", "execute",
        "run", "tool", "搜索", "浏览", "数据库", "文件", "表格",
        "执行", "运行", "工具",
    ))

    scores = {
        "reasoning": _bounded(0.1, 0.17, reasoning_hits),
        "context": _bounded(0.08, 0.18, context_hits),
        "coding": _bounded(0.05, 0.18, coding_hits),
        "output": _bounded(0.1, 0.16, output_hits),
        "tool_need": _bounded(0.0, 0.2, tool_hits),
    }
    if char_count >= 1000:
        scores["context"] = max(scores["context"], 0.85)
        explanations.append("long input context detected")
    elif char_count >= 400:
        scores["context"] = max(scores["context"], 0.6)
        explanations.append("substantial input context detected")
    if reasoning_hits >= 2:
        explanations.append("multi-step reasoning indicators detected")
    if coding_hits:
        explanations.append("coding or implementation requested")
    if output_hits >= 2:
        explanations.append("structured or extended output requested")
    if tool_hits:
        explanations.append("external tool interaction requested")
    if context_hits >= 2:
        explanations.append("research or long-context work requested")

    weights = policy["aggregate_weights"]
    aggregate = round(sum(
        float(weights[key]) * scores[key] for key in DIMENSIONS
    ), 6)
    thresholds = policy["level_thresholds"]
    if aggregate < float(thresholds["low"]["maximum_exclusive"]):
        level = "low"
    elif aggregate < float(thresholds["medium"]["maximum_exclusive"]):
        level = "medium"
    else:
        level = "high"
    if not explanations:
        explanations.append("short request with no complex-work indicators")
    return {
        "complexity_level": level,
        "scores": scores,
        "aggregate_score": aggregate,
        "explanation": explanations,
        "policy_version": policy["policy_version"],
        "source_type": "offline_deterministic_rules",
    }


def build_examples() -> dict[str, Any]:
    scenarios = {
        "simple_question": "What is photosynthesis?",
        "coding_task": (
            "Write Python code for an algorithm, implement a function, and debug it."
        ),
        "research_task": (
            "Research multiple sources, compare competing explanations, analyze "
            "the trade-offs step by step, and produce a comprehensive report "
            "with sections, a table, and conclusions for a long document."
        ),
        "agent_task": (
            "Search files, browse research sources, and query a database; analyze "
            "and compare the evidence step by step, execute multiple tools, then "
            "write a comprehensive detailed report with sections and a review."
        ),
    }
    return {
        "demonstration_type": "offline_only",
        "real_api_calls_performed": 0,
        "examples": [
            {"scenario_id": key, "prompt": value, **analyze_complexity(value)}
            for key, value in scenarios.items()
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    payload = build_examples()
    if not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({
        "status": "ok", "example_count": len(payload["examples"]),
        "dry_run": args.dry_run, "real_api_calls_performed": 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
