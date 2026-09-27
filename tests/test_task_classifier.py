import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from task_classifier import (  # noqa: E402
    build_task_profile,
    classify_task,
    extract_requirements,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Write Python code and debug this function", "coding"),
        ("Explain transformer architecture and analyze why it works", "reasoning"),
        ("Calculate this algebra equation and provide a proof", "mathematics"),
        ("Write an email and rewrite this article", "writing"),
        ("Summarize this long document", "long_document"),
        ("Analyze this image and video", "multimodal"),
        ("Build an agent workflow with tool calling", "agent"),
    ],
)
def test_supported_task_classification(text, expected):
    assert classify_task(text) == expected


def test_requirements_are_bounded_and_deterministic():
    text = "Explain transformer architecture"
    first = extract_requirements(text)
    assert first == extract_requirements(text)
    assert first["reasoning"] >= 0.8
    assert all(0 <= value <= 1 for value in first.values())


def test_chinese_text_adds_language_requirement():
    profile = build_task_profile("请写一篇中文文章")
    assert profile["task_type"] == "writing"
    assert profile["requirements"]["chinese"] == 0.6
    assert profile["source_type"] == "offline_keyword_heuristic"


def test_empty_text_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        classify_task("  ")
