"""Deterministic offline reflection over agent and evaluation results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "reflection_policy_v1.json"


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        policy = json.load(handle)
    threshold = float(policy.get("accept_threshold", -1))
    rounds = int(policy.get("max_reflection_rounds", -1))
    if not 0 <= threshold <= 1 or rounds < 0:
        raise ValueError("invalid reflection threshold or round limit")
    if type(policy.get("retry_enabled")) is not bool:
        raise ValueError("retry_enabled must be boolean")
    return policy


def _correction_actions(
    task_type: str, evaluation: Mapping[str, Any]
) -> list[str]:
    issues = " ".join(map(str, evaluation.get("issues", []))).casefold()
    actions = []
    if task_type == "coding":
        return ["coding_error"]
    if "format" in issues or "structure" in issues:
        actions.append("format_error")
    if "missing" in issues:
        actions.append("missing_content")
    if (
        "incomplete" in issues
        or "shorter" in issues
        or not actions
    ):
        actions.append("incomplete_answer")
    return list(dict.fromkeys(actions))


def reflect_execution(
    payload: Mapping[str, Any],
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether an evaluated result should be accepted or corrected."""
    policy = dict(policy or load_policy())
    threshold = float(policy["accept_threshold"])
    maximum = int(policy["max_reflection_rounds"])
    evaluation = payload.get("evaluation_result", {})
    score = float(evaluation.get("evaluation_score", -1))
    round_number = int(payload.get("reflection_round", 0))
    task_type = str(payload.get("task_type", ""))
    if not 0 <= score <= 1:
        raise ValueError("evaluation_score must be in [0,1]")
    if round_number < 0:
        raise ValueError("reflection_round must be non-negative")

    distance = abs(score - threshold)
    confidence = round(min(0.99, 0.7 + distance * 0.5), 3)
    base = {
        "confidence": confidence,
        "evaluation_score": score,
        "accept_threshold": threshold,
        "reflection_round": round_number,
        "policy_version": policy["policy_version"],
        "source_type": "offline_deterministic_reflection",
    }
    if score >= threshold:
        return {
            **base,
            "decision": "accept",
            "reason": ["evaluation score meets configured threshold"],
            "correction_plan": [],
        }
    issues = [str(issue) for issue in evaluation.get("issues", [])]
    if not policy["retry_enabled"] or round_number >= maximum:
        return {
            **base,
            "decision": "retry_limit_reached",
            "reason": issues or ["evaluation below threshold and retry unavailable"],
            "correction_plan": [],
        }
    return {
        **base,
        "decision": "correct",
        "reason": issues or ["evaluation score below configured threshold"],
        "correction_plan": _correction_actions(task_type, evaluation),
    }
