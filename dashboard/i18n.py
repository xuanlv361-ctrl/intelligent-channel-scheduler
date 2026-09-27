"""UI translations for the offline Streamlit dashboard."""

from __future__ import annotations

from typing import Any


LANGUAGE_OPTIONS = {
    "English": "en",
    "中文": "zh",
    "日本語": "ja",
}


TRANSLATIONS = {
    "en": {
        "app_title": "Intelligent LLM Gateway Observability",
        "language": "Language",
        "offline_caption": "Offline demonstration only · bounded local JSON/JSONL snapshots · no real API",
        "tab_channel": "Channel Overview",
        "tab_routing": "Routing Decision History",
        "tab_failure": "Failure Analysis",
        "tab_strategy": "Strategy Comparison",
        "tab_selection": "Model Selection",
        "tab_feedback": "Model Feedback",
        "tab_evaluation": "Model Evaluation",
        "tab_tools": "Tool Usage",
        "tab_reflection": "Reflection History",
        "channels": "Channels", "healthy": "Healthy", "average_health": "Average health",
        "configured_health": "Configured channel health", "no_health": "No channel health snapshot is available.",
        "latest_history": "Latest bounded decision history", "no_decisions": "No decision logs are available.",
        "failure_summary": "Failure and fallback summary", "no_failures": "No offline recovery failures are available.",
        "strategy_comparison": "Offline strategy comparison", "no_strategy": "No strategy comparison output is available.",
        "selection_decisions": "Offline model selection decisions", "no_selection": "No offline model selection log is available.",
        "decision": "Decision", "user_request": "User request", "detected_task": "Detected task",
        "selected_model": "Selected model", "final_channel": "Final channel", "model_ranking": "Model ranking",
        "channel_candidates": "Channel candidates", "none": "none",
        "feedback": "Offline model feedback", "no_feedback": "No offline feedback log is available.",
        "feedback_records": "Feedback records", "models_represented": "Models represented",
        "model_usage": "Model usage and outcomes", "task_distribution": "Task distribution",
        "feedback_note": "Synthetic offline feedback fixture; analysis does not update model weights.",
        "evaluation": "Offline model evaluation", "no_evaluation": "No offline evaluation results are available.",
        "evaluated_responses": "Evaluated responses", "average_score": "Average evaluation score",
        "evaluation_results": "Model and task evaluation results", "common_issues": "Common issues",
        "no_issues": "No issues were detected by the configured rules.",
        "evaluation_note": "Synthetic offline responses evaluated by deterministic heuristics; scores are not human or benchmark judgments.",
        "tool_usage": "Offline tool usage", "no_tools": "No offline tool calls are available.",
        "tool_calls": "Tool calls", "execution_trace": "Execution trace",
        "tool_note": "Fixed-time deterministic mock executions from the local agent trace; no external tools or APIs were called.",
        "reflection": "Offline reflection history", "no_reflection": "No offline reflection traces are available.",
        "reflected_results": "Reflected agent results", "accepted_results": "Accepted final results",
        "reflection_note": "Deterministic rule evaluation and correction history; no model was called during reflection.",
        "sources": "Sources: output/*.json, bounded decision logs, and existing simulation results.",
    },
    "zh": {
        "app_title": "智能大模型网关监控台", "language": "语言",
        "offline_caption": "仅供离线演示 · 使用有界本地 JSON/JSONL 快照 · 不调用真实 API",
        "tab_channel": "渠道概览", "tab_routing": "路由决策历史", "tab_failure": "故障分析",
        "tab_strategy": "策略对比", "tab_selection": "模型选择", "tab_feedback": "模型反馈",
        "tab_evaluation": "模型评估", "tab_tools": "工具使用", "tab_reflection": "反思历史",
        "channels": "渠道数", "healthy": "健康渠道", "average_health": "平均健康分",
        "configured_health": "已配置的渠道健康状态", "no_health": "暂无渠道健康快照。",
        "latest_history": "最近的有界决策历史", "no_decisions": "暂无决策日志。",
        "failure_summary": "故障与回退摘要", "no_failures": "暂无离线恢复故障记录。",
        "strategy_comparison": "离线策略对比", "no_strategy": "暂无策略对比结果。",
        "selection_decisions": "离线模型选择决策", "no_selection": "暂无离线模型选择日志。",
        "decision": "决策", "user_request": "用户请求", "detected_task": "识别的任务",
        "selected_model": "选中的模型", "final_channel": "最终渠道", "model_ranking": "模型排名",
        "channel_candidates": "候选渠道", "none": "无",
        "feedback": "离线模型反馈", "no_feedback": "暂无离线反馈日志。",
        "feedback_records": "反馈记录数", "models_represented": "涉及模型数",
        "model_usage": "模型使用与结果", "task_distribution": "任务分布",
        "feedback_note": "合成离线反馈样例；分析不会更新模型权重。",
        "evaluation": "离线模型评估", "no_evaluation": "暂无离线评估结果。",
        "evaluated_responses": "已评估响应", "average_score": "平均评估分",
        "evaluation_results": "模型与任务评估结果", "common_issues": "常见问题",
        "no_issues": "配置规则未检测到问题。",
        "evaluation_note": "确定性启发式规则评估的合成离线响应；分数不代表人工判断或基准测试。",
        "tool_usage": "离线工具使用", "no_tools": "暂无离线工具调用。",
        "tool_calls": "工具调用数", "execution_trace": "执行轨迹",
        "tool_note": "来自本地 Agent 轨迹的固定耗时确定性模拟；未调用外部工具或 API。",
        "reflection": "离线反思历史", "no_reflection": "暂无离线反思轨迹。",
        "reflected_results": "已反思的 Agent 结果", "accepted_results": "最终接受结果",
        "reflection_note": "确定性规则评估与修正历史；反思过程未调用模型。",
        "sources": "数据源：output/*.json、有界决策日志及现有模拟结果。",
    },
    "ja": {
        "app_title": "インテリジェントLLMゲートウェイ監視", "language": "言語",
        "offline_caption": "オフラインデモ専用 · ローカルのJSON/JSONLスナップショット · 実APIは未使用",
        "tab_channel": "チャネル概要", "tab_routing": "ルーティング履歴", "tab_failure": "障害分析",
        "tab_strategy": "戦略比較", "tab_selection": "モデル選択", "tab_feedback": "モデルフィードバック",
        "tab_evaluation": "モデル評価", "tab_tools": "ツール利用", "tab_reflection": "振り返り履歴",
        "channels": "チャネル数", "healthy": "正常", "average_health": "平均ヘルススコア",
        "configured_health": "設定済みチャネルヘルス", "no_health": "チャネルヘルスのスナップショットがありません。",
        "latest_history": "最新の決定履歴", "no_decisions": "決定ログがありません。",
        "failure_summary": "障害とフォールバックの概要", "no_failures": "オフライン復旧障害はありません。",
        "strategy_comparison": "オフライン戦略比較", "no_strategy": "戦略比較結果がありません。",
        "selection_decisions": "オフラインモデル選択", "no_selection": "モデル選択ログがありません。",
        "decision": "決定", "user_request": "ユーザーリクエスト", "detected_task": "検出タスク",
        "selected_model": "選択モデル", "final_channel": "最終チャネル", "model_ranking": "モデル順位",
        "channel_candidates": "候補チャネル", "none": "なし",
        "feedback": "オフラインモデルフィードバック", "no_feedback": "フィードバックログがありません。",
        "feedback_records": "フィードバック数", "models_represented": "対象モデル数",
        "model_usage": "モデル利用と結果", "task_distribution": "タスク分布",
        "feedback_note": "合成オフラインデータです。分析によるモデル重みの更新はありません。",
        "evaluation": "オフラインモデル評価", "no_evaluation": "評価結果がありません。",
        "evaluated_responses": "評価済み応答", "average_score": "平均評価スコア",
        "evaluation_results": "モデル・タスク評価結果", "common_issues": "共通の問題",
        "no_issues": "設定ルールでは問題が検出されませんでした。",
        "evaluation_note": "決定論的ヒューリスティックによる合成応答の評価であり、人手評価やベンチマークではありません。",
        "tool_usage": "オフラインツール利用", "no_tools": "ツール呼び出しがありません。",
        "tool_calls": "ツール呼び出し数", "execution_trace": "実行トレース",
        "tool_note": "固定時間の決定論的モック実行です。外部ツールやAPIは呼び出していません。",
        "reflection": "オフライン振り返り履歴", "no_reflection": "振り返りトレースがありません。",
        "reflected_results": "振り返り済み結果", "accepted_results": "最終承認結果",
        "reflection_note": "決定論的ルールによる評価・修正履歴です。振り返り時にモデルは呼び出していません。",
        "sources": "データソース：output/*.json、限定された決定ログ、既存のシミュレーション結果。",
    },
}


COLUMN_LABELS = {
    "en": {},
    "zh": {
        "channel_id": "渠道ID", "health_score": "健康分", "confidence": "置信度", "status": "状态",
        "latency_ms": "延迟（毫秒）", "success_rate": "成功率", "request_id": "请求ID",
        "selected_channel": "选中渠道", "strategy": "策略", "score": "得分", "reason": "原因",
        "error_type": "错误类型", "channel": "渠道", "frequency": "次数", "fallback_count": "回退次数",
        "cost": "成本", "regret": "遗憾值", "model": "模型", "usage_count": "使用次数",
        "average_rating": "平均评分", "average_latency_ms": "平均延迟（毫秒）", "task_type": "任务类型",
        "count": "数量", "task": "任务", "evaluation_score": "评估分", "issues": "问题",
        "issue": "问题", "tool_name": "工具名称", "agent_role": "Agent角色", "execution_count": "执行次数",
        "average_execution_time": "平均执行时间", "failure_reason": "失败原因", "execution_id": "执行ID",
        "reflection_decision": "反思决策", "correction_action": "修正动作", "final_status": "最终状态",
        "candidate_id": "候选ID", "candidate_name": "候选名称",
    },
    "ja": {
        "channel_id": "チャネルID", "health_score": "ヘルススコア", "confidence": "信頼度", "status": "状態",
        "latency_ms": "遅延（ms）", "success_rate": "成功率", "request_id": "リクエストID",
        "selected_channel": "選択チャネル", "strategy": "戦略", "score": "スコア", "reason": "理由",
        "error_type": "エラー種別", "channel": "チャネル", "frequency": "回数", "fallback_count": "フォールバック数",
        "cost": "コスト", "regret": "リグレット", "model": "モデル", "usage_count": "利用回数",
        "average_rating": "平均評価", "average_latency_ms": "平均遅延（ms）", "task_type": "タスク種別",
        "count": "件数", "task": "タスク", "evaluation_score": "評価スコア", "issues": "問題",
        "issue": "問題", "tool_name": "ツール名", "agent_role": "エージェント役割", "execution_count": "実行回数",
        "average_execution_time": "平均実行時間", "failure_reason": "失敗理由", "execution_id": "実行ID",
        "reflection_decision": "振り返り判断", "correction_action": "修正アクション", "final_status": "最終状態",
        "candidate_id": "候補ID", "candidate_name": "候補名",
    },
}


VALUE_LABELS = {
    "en": {},
    "zh": {"healthy": "健康", "warning": "警告", "degraded": "降级", "accepted": "已接受", "accept": "接受", "none": "无"},
    "ja": {"healthy": "正常", "warning": "警告", "degraded": "低下", "accepted": "承認済み", "accept": "承認", "none": "なし"},
}


def text(language: str, key: str) -> str:
    return TRANSLATIONS.get(language, TRANSLATIONS["en"]).get(key, key)


def localize_rows(rows: list[dict[str, Any]], language: str) -> list[dict[str, Any]]:
    columns = COLUMN_LABELS.get(language, {})
    values = VALUE_LABELS.get(language, {})
    return [
        {
            columns.get(key, key): values.get(value, value)
            if isinstance(value, str) else value
            for key, value in row.items()
        }
        for row in rows
    ]
