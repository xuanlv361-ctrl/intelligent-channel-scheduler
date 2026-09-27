"""Human-readable, non-mutating routing recommendation text."""

from __future__ import annotations

from typing import Any, Mapping


def format_routing_suggestion(comparison: Mapping[str, Any]) -> str:
    task = comparison["task_type"]
    confidence = comparison["confidence"]
    current = comparison["current_preference"]
    candidate = comparison["best_model"]
    action = comparison["action"]
    if action == "increase_candidate_priority":
        return (
            f"For {task}, consider increasing {candidate} candidate priority "
            f"relative to {current} ({confidence} confidence); validate in a "
            "controlled offline experiment before any routing change."
        )
    if action == "insufficient_samples":
        return (
            f"For {task}, retain {current} and collect more comparable samples "
            "before changing candidate priority."
        )
    return (
        f"For {task}, retain {current}; the available offline metrics do not "
        "show a sufficiently large alternative-model advantage."
    )
