import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_planner import plan_agents


def test_simple_question_uses_single_model():
    plan = plan_agents({
        "task_type": "qa",
        "complexity_level": "low",
        "scores": {"reasoning": 0.1},
    })
    assert plan["execution_mode"] == "single_model"
    assert plan["agents"] == []


def test_complex_research_uses_multiple_agents():
    plan = plan_agents({
        "task_type": "research",
        "complexity_level": "high",
        "scores": {"context": 0.9, "reasoning": 0.8, "output": 0.8},
    })
    assert plan["execution_mode"] == "multi_agent"
    assert [row["role"] for row in plan["agents"]] == [
        "research_agent", "reasoning_agent", "writer_agent", "review_agent"
    ]


def test_agent_ordering_is_deterministic():
    profile = {
        "task_type": "coding",
        "complexity": {
            "complexity_level": "high",
            "scores": {"coding": 0.9, "reasoning": 0.8},
        },
    }
    first = plan_agents(profile)
    assert first == plan_agents(profile)
    assert [row["role"] for row in first["agents"]] == [
        "coding_agent", "reasoning_agent", "review_agent"
    ]
