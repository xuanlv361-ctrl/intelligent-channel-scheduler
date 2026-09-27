import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prompt_complexity_analyzer import analyze_complexity, build_examples


def test_simple_question_is_low():
    result = analyze_complexity("What is photosynthesis?")
    assert result["complexity_level"] == "low"


def test_coding_request_is_medium():
    result = analyze_complexity(
        "Write Python code for an algorithm, implement a function, and debug it."
    )
    assert result["complexity_level"] == "medium"
    assert result["scores"]["coding"] >= 0.7


def test_research_request_is_high():
    result = analyze_complexity(
        "Research multiple sources and literature, analyze and compare the "
        "trade-offs step by step, then produce a comprehensive detailed report "
        "with sections and a table for a long document."
    )
    assert result["complexity_level"] == "high"


def test_output_is_deterministic_and_examples_are_offline():
    prompt = "Search files, analyze evidence, and write a detailed report."
    assert analyze_complexity(prompt) == analyze_complexity(prompt)
    examples = build_examples()
    assert len(examples["examples"]) == 4
    assert examples["real_api_calls_performed"] == 0
    json.dumps(examples)
