"""Deterministic keyword-based offline task classifier."""

from __future__ import annotations

from typing import Any


CLASSIFIER_VERSION = "task-classifier-v1.0.0"
TASK_ORDER = (
    "coding", "reasoning", "mathematics", "writing",
    "long_document", "multimodal", "agent",
)
KEYWORDS = {
    "coding": ("code", "python", "bug", "debug", "function", "api", "代码", "编程", "调试"),
    "reasoning": ("explain", "analyze", "reason", "architecture", "why", "解释", "分析", "推理"),
    "mathematics": ("math", "equation", "calculate", "proof", "algebra", "数学", "公式", "计算", "证明"),
    "writing": ("write", "rewrite", "article", "email", "story", "写作", "文章", "改写", "邮件"),
    "long_document": ("long document", "long context", "summarize document", "report", "长文档", "长上下文", "报告摘要"),
    "multimodal": ("image", "video", "audio", "diagram", "图片", "图像", "视频", "音频"),
    "agent": ("agent", "tool calling", "workflow", "automate", "代理", "工具调用", "工作流", "自动化"),
}


def _keyword_scores(text: str) -> tuple[dict[str, float], dict[str, list[str]]]:
    lowered = text.lower()
    raw = {}
    matched = {}
    for task in TASK_ORDER:
        hits = [keyword for keyword in KEYWORDS[task] if keyword in lowered]
        matched[task] = hits
        raw[task] = min(len(hits) * 0.35, 1.0)
    if not any(raw.values()):
        raw["reasoning"] = 0.4
    return raw, matched


def classify_task(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("request text must be a non-empty string")
    scores, _ = _keyword_scores(text)
    return max(TASK_ORDER, key=lambda task: (scores[task], -TASK_ORDER.index(task)))


def extract_requirements(text: str) -> dict[str, float]:
    task = classify_task(text)
    scores, _ = _keyword_scores(text)
    scores[task] = max(scores[task], 0.8)
    requirements = {
        name: round(value, 3) for name, value in scores.items() if value > 0
    }
    if any("\u4e00" <= character <= "\u9fff" for character in text):
        requirements["chinese"] = 0.6
    return requirements


def build_task_profile(text: str) -> dict[str, Any]:
    task = classify_task(text)
    _, matched = _keyword_scores(text)
    return {
        "task_type": task,
        "requirements": extract_requirements(text),
        "matched_keywords": matched[task],
        "classifier_version": CLASSIFIER_VERSION,
        "source_type": "offline_keyword_heuristic",
    }
