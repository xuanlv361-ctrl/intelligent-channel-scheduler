"""Streamlit observability dashboard for bounded offline demonstration outputs."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import fmean
from typing import Any

try:
    from dashboard.i18n import LANGUAGE_OPTIONS, localize_rows, text
except ModuleNotFoundError:  # Direct execution: streamlit run dashboard/app.py
    from i18n import LANGUAGE_OPTIONS, localize_rows, text


ROOT = Path(__file__).resolve().parents[1]
OFFLINE_LABEL = "offline demonstration only"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows[-limit:]


def load_channel_overview(root: Path = ROOT) -> list[dict[str, Any]]:
    health = read_json(
        root / "output" / "channel_health_simulation_snapshot_v1.json",
        {"channels": []},
    )
    metrics = read_json(
        root / "data" / "channel_health_simulation_metrics_v1.json",
        {"channels": []},
    )
    metric_by_id = {str(row["channel_id"]): row for row in metrics["channels"]}
    rows = []
    for row in health["channels"]:
        source = metric_by_id.get(str(row["channel_id"]), {"metrics": {}})
        values = source["metrics"]
        rows.append({
            "channel_id": row["channel_id"],
            "health_score": row["health_score"],
            "confidence": row["confidence"],
            "status": row["status"],
            "latency_ms": values.get("avg_latency_ms"),
            "success_rate": values.get("success_rate"),
        })
    return rows


def load_routing_history(root: Path = ROOT) -> list[dict[str, Any]]:
    sources = [
        root / "output" / "scheduler_decision_logs_v1.jsonl",
        root / "output" / "real_shadow_decision_logs_v1.jsonl",
    ]
    rows = []
    for source in sources:
        for row in read_jsonl(source, 100):
            ranking = row.get("candidate_ranking") or row.get("ranking") or []
            selected = row.get("recommended_candidate") or row.get("selected_candidate")
            selected_row = next(
                (item for item in ranking if item.get("candidate_id") == selected), {}
            )
            rows.append({
                "request_id": row.get("request_id"),
                "selected_channel": selected,
                "strategy": row.get("strategy"),
                "score": selected_row.get("final_score"),
                "reason": row.get("selection_reason")
                or row.get("recommendation_scope")
                or row.get("outcome"),
            })
    return rows[-100:]


def load_failure_analysis(root: Path = ROOT) -> list[dict[str, Any]]:
    demo = read_json(
        root / "output" / "execution_recovery_demo_v1.json", {"trace": {"attempts": []}}
    )
    attempts = demo.get("trace", {}).get("attempts", [])
    counts = Counter(
        (str(row.get("error")), str(row.get("channel")))
        for row in attempts if row.get("result") == "failed"
    )
    fallback_count = int(bool(demo.get("trace", {}).get("fallback_executed")))
    return [
        {
            "error_type": error,
            "channel": channel,
            "frequency": frequency,
            "fallback_count": fallback_count,
        }
        for (error, channel), frequency in sorted(counts.items())
    ]


def load_strategy_comparison(root: Path = ROOT) -> list[dict[str, Any]]:
    payload = read_json(
        root / "output" / "health_aware_strategy_comparison_v1.json",
        {"summary": []},
    )
    return [
        {
            "strategy": row["strategy"],
            "success_rate": row["success_rate"],
            "latency_ms": row["average_latency_ms"],
            "cost": row["average_cost"],
            "regret": row["average_regret"],
        }
        for row in payload.get("summary", [])
    ]


def load_model_selection(root: Path = ROOT) -> list[dict[str, Any]]:
    return read_jsonl(
        root / "output" / "model_selection_decisions_v1.jsonl", limit=20
    )


def load_model_feedback(root: Path = ROOT) -> dict[str, Any]:
    payload = read_json(
        root / "data" / "user_feedback_v1.json", {"records": []}
    )
    records = payload.get("records", [])
    by_model: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        by_model.setdefault(row["selected_model"], []).append(row)
    model_summary = [
        {
            "model": model,
            "usage_count": len(rows),
            "success_rate": sum(row["success"] for row in rows) / len(rows),
            "average_rating": fmean(row["user_rating"] for row in rows),
            "average_latency_ms": fmean(row["latency_ms"] for row in rows),
        }
        for model, rows in sorted(by_model.items())
    ]
    task_distribution = [
        {"task_type": task, "count": count}
        for task, count in sorted(Counter(
            row["task_type"] for row in records
        ).items())
    ]
    return {
        "source_type": payload.get("source_type"),
        "record_count": len(records),
        "model_summary": model_summary,
        "task_distribution": task_distribution,
    }


def load_model_evaluations(root: Path = ROOT) -> dict[str, Any]:
    payload = read_json(
        root / "output" / "model_evaluation_results_v1.json",
        {"records": []},
    )
    records = payload.get("records", [])
    issue_counts = Counter(
        issue for row in records for issue in row.get("issues", [])
    )
    return {
        "source_type": payload.get("source_type"),
        "record_count": len(records),
        "average_score": (
            fmean(float(row["evaluation_score"]) for row in records)
            if records else None
        ),
        "records": [
            {
                "model": row["model"],
                "task": row["task_type"],
                "evaluation_score": row["evaluation_score"],
                "issues": ", ".join(row.get("issues", [])) or "none",
            }
            for row in records
        ],
        "common_issues": [
            {"issue": issue, "frequency": count}
            for issue, count in sorted(
                issue_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }


def load_tool_usage(root: Path = ROOT) -> dict[str, Any]:
    trace = read_json(
        root / "output" / "agent_execution_trace_v1.json",
        {"agents": []},
    )
    catalog = read_json(
        root / "data" / "tool_catalog_v1.json",
        {"tools": []},
    )
    names = {
        row["tool_id"]: row["tool_name"] for row in catalog.get("tools", [])
    }
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for agent in trace.get("agents", []):
        for result in agent.get("tool_results", []):
            key = (str(result["tool_id"]), str(agent["role"]))
            groups.setdefault(key, []).append(result)
    rows = []
    for (tool_id, role), results in sorted(groups.items()):
        failures = sorted({
            str(row["failure_reason"]) for row in results
            if row.get("failure_reason")
        })
        rows.append({
            "tool_name": names.get(tool_id, tool_id),
            "agent_role": role,
            "execution_count": len(results),
            "success_rate": (
                sum(row["status"] == "success" for row in results) / len(results)
            ),
            "average_execution_time": fmean(
                float(row["execution_time"]) for row in results
            ),
            "failure_reason": ", ".join(failures) or "none",
        })
    return {
        "execution_id": trace.get("execution_id"),
        "source_type": "offline_agent_execution_trace",
        "rows": rows,
        "total_tool_calls": sum(row["execution_count"] for row in rows),
    }


def load_reflection_history(root: Path = ROOT) -> list[dict[str, Any]]:
    trace = read_json(
        root / "output" / "agent_execution_trace_v1.json",
        {"agents": []},
    )
    rows = []
    for agent in trace.get("agents", []):
        evaluation = agent.get("final_evaluation")
        reflection = agent.get("final_reflection")
        if not evaluation or not reflection:
            continue
        evaluation_steps = [
            step for step in agent.get("steps", [])
            if step.get("type") == "evaluation"
        ]
        corrections = [
            action for step in agent.get("steps", [])
            if step.get("type") == "correction"
            for action in step.get("actions", [])
        ]
        rows.append({
            "execution_id": trace.get("execution_id"),
            "agent_role": agent["role"],
            "task_type": (
                evaluation_steps[-1].get("task_type")
                if evaluation_steps else "unknown"
            ),
            "evaluation_score": evaluation["evaluation_score"],
            "reflection_decision": reflection["decision"],
            "correction_action": ", ".join(corrections) or "none",
            "final_status": agent.get("final_status", "unknown"),
        })
    return rows


def load_dashboard_data(root: Path = ROOT) -> dict[str, Any]:
    return {
        "channel_overview": load_channel_overview(root),
        "routing_history": load_routing_history(root),
        "failure_analysis": load_failure_analysis(root),
        "strategy_comparison": load_strategy_comparison(root),
        "model_selection": load_model_selection(root),
        "model_feedback": load_model_feedback(root),
        "model_evaluations": load_model_evaluations(root),
        "tool_usage": load_tool_usage(root),
        "reflection_history": load_reflection_history(root),
        "source_label": OFFLINE_LABEL,
    }


def render_dashboard(st_module=None) -> None:
    if st_module is None:
        import streamlit as st_module
    st = st_module
    st.set_page_config(page_title="LLM Gateway Observability", layout="wide")
    title_column, language_column = st.columns([5, 2])
    with language_column:
        language_label = st.radio(
            "Language / 语言 / 言語",
            options=list(LANGUAGE_OPTIONS),
            horizontal=True,
            label_visibility="collapsed",
            key="dashboard_language",
        )
    language = LANGUAGE_OPTIONS[language_label]
    t = lambda key: text(language, key)
    table = lambda rows: st.table(localize_rows(rows, language))
    with title_column:
        st.title(t("app_title"))
    st.caption(t("offline_caption"))
    data = load_dashboard_data()
    (
        overview, routing, failures, comparison, model_selection,
        model_feedback, model_evaluation, tool_usage, reflection_history,
    ) = st.tabs([
        t("tab_channel"), t("tab_routing"), t("tab_failure"),
        t("tab_strategy"), t("tab_selection"), t("tab_feedback"),
        t("tab_evaluation"), t("tab_tools"), t("tab_reflection"),
    ])
    with overview:
        rows = data["channel_overview"]
        if rows:
            healthy = sum(row["status"] == "healthy" for row in rows)
            cols = st.columns(3)
            cols[0].metric(t("channels"), len(rows))
            cols[1].metric(t("healthy"), healthy)
            cols[2].metric(
                t("average_health"),
                f"{sum(row['health_score'] for row in rows) / len(rows):.3f}",
            )
            st.subheader(t("configured_health"))
            table(rows)
        else:
            st.info(t("no_health"))
    with routing:
        st.subheader(t("latest_history"))
        if data["routing_history"]:
            table(data["routing_history"])
        else:
            st.info(t("no_decisions"))
    with failures:
        st.subheader(t("failure_summary"))
        if data["failure_analysis"]:
            table(data["failure_analysis"])
        else:
            st.info(t("no_failures"))
    with comparison:
        st.subheader(t("strategy_comparison"))
        if data["strategy_comparison"]:
            table(data["strategy_comparison"])
        else:
            st.info(t("no_strategy"))
    with model_selection:
        st.subheader(t("selection_decisions"))
        rows = data["model_selection"]
        if not rows:
            st.info(t("no_selection"))
        else:
            selected = st.selectbox(
                t("decision"),
                options=list(range(len(rows))),
                format_func=lambda index, decision_rows=rows: (
                    decision_rows[index]["request_id"]
                ),
            )
            row = rows[selected]
            st.markdown(f"**{t('user_request')}**")
            st.write(row["user_request"])
            cols = st.columns(3)
            cols[0].metric(t("detected_task"), row["detected_task"]["task_type"])
            cols[1].metric(t("selected_model"), row["selected_model"])
            cols[2].metric(t("final_channel"), row["final_channel_decision"] or t("none"))
            st.markdown(f"**{t('model_ranking')}**")
            table([
                {"model": item["model"], "score": item["score"]}
                for item in row["model_ranking"]
            ])
            st.markdown(f"**{t('channel_candidates')}**")
            table(row["channel_candidates"])
    with model_feedback:
        st.subheader(t("feedback"))
        feedback = data["model_feedback"]
        if not feedback["record_count"]:
            st.info(t("no_feedback"))
        else:
            cols = st.columns(2)
            cols[0].metric(t("feedback_records"), feedback["record_count"])
            cols[1].metric(
                t("models_represented"), len(feedback["model_summary"])
            )
            st.markdown(f"**{t('model_usage')}**")
            table(feedback["model_summary"])
            st.markdown(f"**{t('task_distribution')}**")
            table(feedback["task_distribution"])
            st.caption(t("feedback_note"))
    with model_evaluation:
        st.subheader(t("evaluation"))
        evaluations = data["model_evaluations"]
        if not evaluations["record_count"]:
            st.info(t("no_evaluation"))
        else:
            cols = st.columns(2)
            cols[0].metric(t("evaluated_responses"), evaluations["record_count"])
            cols[1].metric(
                t("average_score"),
                f"{evaluations['average_score']:.3f}",
            )
            st.markdown(f"**{t('evaluation_results')}**")
            table(evaluations["records"])
            st.markdown(f"**{t('common_issues')}**")
            if evaluations["common_issues"]:
                table(evaluations["common_issues"])
            else:
                st.info(t("no_issues"))
            st.caption(t("evaluation_note"))
    with tool_usage:
        st.subheader(t("tool_usage"))
        usage = data["tool_usage"]
        if not usage["rows"]:
            st.info(t("no_tools"))
        else:
            cols = st.columns(2)
            cols[0].metric(t("tool_calls"), usage["total_tool_calls"])
            cols[1].metric(t("execution_trace"), usage["execution_id"] or t("none"))
            table(usage["rows"])
            st.caption(t("tool_note"))
    with reflection_history:
        st.subheader(t("reflection"))
        rows = data["reflection_history"]
        if not rows:
            st.info(t("no_reflection"))
        else:
            accepted = sum(row["final_status"] == "accepted" for row in rows)
            cols = st.columns(2)
            cols[0].metric(t("reflected_results"), len(rows))
            cols[1].metric(t("accepted_results"), accepted)
            table(rows)
            st.caption(t("reflection_note"))
    st.caption(t("sources"))


if __name__ == "__main__":
    render_dashboard()
