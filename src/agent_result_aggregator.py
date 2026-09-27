"""Ordered aggregation of offline agent results."""

from __future__ import annotations

from typing import Any, Mapping


def aggregate_results(
    results: list[Mapping[str, Any]],
    *,
    method: str = "ordered_sections",
) -> dict[str, Any]:
    if method != "ordered_sections":
        raise ValueError(f"unsupported aggregation method: {method}")
    successful = [row for row in results if row.get("status") == "success"]
    failed = [str(row.get("role")) for row in results if row.get("status") != "success"]
    sections = [
        {
            "role": row["role"],
            "model": row["selected_model"],
            "content": row["output"],
        }
        for row in successful
    ]
    final_response = "\n\n".join(
        f"## {row['role']}\n{row['output']}" for row in successful
    )
    return {
        "aggregation_method": method,
        "sections": sections,
        "final_response": final_response,
        "successful_agent_count": len(successful),
        "failed_agents": failed,
    }
