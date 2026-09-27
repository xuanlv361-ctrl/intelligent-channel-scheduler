import importlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_dashboard_module_loads_without_side_effects():
    app = importlib.import_module("dashboard.app")
    assert callable(app.load_dashboard_data)
    assert callable(app.render_dashboard)


def test_dashboard_data_parsing():
    from dashboard.app import load_dashboard_data
    data = load_dashboard_data(ROOT)
    assert data["source_label"] == "offline demonstration only"
    assert len(data["channel_overview"]) == 8
    assert {row["status"] for row in data["channel_overview"]} >= {
        "healthy", "warning", "degraded"
    }
    assert {row["strategy"] for row in data["strategy_comparison"]} == {
        "confidence_aware_v2", "health_aware_v1"
    }
    assert data["failure_analysis"][0]["error_type"] == "timeout"
    assert data["failure_analysis"][0]["fallback_count"] == 1
    assert len(data["model_selection"]) == 3
    assert {
        "user_request", "detected_task", "model_ranking", "selected_model",
        "channel_candidates", "final_channel_decision",
    } <= data["model_selection"][0].keys()
    assert data["model_feedback"]["record_count"] == 8
    assert data["model_feedback"]["model_summary"]
    assert data["model_evaluations"]["record_count"] == 4
    assert data["model_evaluations"]["records"]
    assert {
        "model", "task", "evaluation_score", "issues"
    } <= data["model_evaluations"]["records"][0].keys()
    assert data["tool_usage"]["total_tool_calls"] >= 1
    assert {
        "tool_name", "agent_role", "execution_count", "success_rate",
        "average_execution_time", "failure_reason",
    } <= data["tool_usage"]["rows"][0].keys()
    assert data["reflection_history"]
    assert {
        "execution_id", "task_type", "evaluation_score",
        "reflection_decision", "correction_action", "final_status",
    } <= data["reflection_history"][0].keys()


def test_streamlit_app_smoke():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / "dashboard" / "app.py"))
    app.run(timeout=15)
    assert not app.exception
    assert len(app.tabs) == 9
    assert len(app.radio) == 1
    assert app.radio[0].options == ["English", "中文", "日本語"]


def test_dashboard_switches_all_navigation_to_chinese():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / "dashboard" / "app.py"))
    app.run(timeout=15)
    app.radio[0].set_value("中文").run(timeout=15)
    assert not app.exception
    assert app.title[0].value == "智能大模型网关监控台"
    assert [tab.label for tab in app.tabs] == [
        "渠道概览", "路由决策历史", "故障分析", "策略对比", "模型选择",
        "模型反馈", "模型评估", "工具使用", "反思历史",
    ]


def test_dashboard_switches_all_navigation_to_japanese():
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(str(ROOT / "dashboard" / "app.py"))
    app.run(timeout=15)
    app.radio[0].set_value("日本語").run(timeout=15)
    assert not app.exception
    assert app.title[0].value == "インテリジェントLLMゲートウェイ監視"
    assert [tab.label for tab in app.tabs] == [
        "チャネル概要", "ルーティング履歴", "障害分析", "戦略比較", "モデル選択",
        "モデルフィードバック", "モデル評価", "ツール利用", "振り返り履歴",
    ]
