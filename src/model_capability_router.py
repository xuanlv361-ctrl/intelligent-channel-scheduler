"""Offline deterministic model ranking by task-requirement dot product."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = ROOT / "data" / "model_capability_catalog_v1.json"


class ModelCapabilityError(ValueError):
    pass


def load_catalog(path: Path = DEFAULT_CATALOG_PATH) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8-sig") as handle:
        catalog = json.load(handle)
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: Mapping[str, Any]) -> None:
    if not catalog.get("models"):
        raise ModelCapabilityError("model catalog must contain models")
    ids = []
    for model in catalog["models"]:
        required = {
            "model_id", "model_name", "provider", "capabilities",
            "supported_tasks", "cost_level", "latency_level",
        }
        missing = required - set(model)
        if missing:
            raise ModelCapabilityError(f"model missing fields: {','.join(sorted(missing))}")
        ids.append(model["model_id"])
        for name, value in model["capabilities"].items():
            if not name.endswith("_score") or not 0 <= float(value) <= 1:
                raise ModelCapabilityError("capability scores must use *_score and be in [0,1]")
    if len(ids) != len(set(ids)):
        raise ModelCapabilityError("model_id must be unique")


def compatibility_score(
    requirements: Mapping[str, Any], model: Mapping[str, Any]
) -> tuple[float, list[dict[str, float]]]:
    explanation = []
    total = 0.0
    for requirement, required_value in sorted(requirements.items()):
        required = float(required_value)
        if not 0 <= required <= 1:
            raise ModelCapabilityError("requirements must be in [0,1]")
        capability = float(model["capabilities"].get(f"{requirement}_score", 0.0))
        contribution = required * capability
        total += contribution
        explanation.append({
            "requirement": requirement,
            "required": required,
            "model_capability": capability,
            "contribution": round(contribution, 6),
        })
    return total, explanation


def route_model(
    task_profile: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    catalog = dict(catalog or load_catalog())
    validate_catalog(catalog)
    requirements = task_profile.get("requirements")
    if not isinstance(requirements, Mapping) or not requirements:
        raise ModelCapabilityError("task profile requires a non-empty requirements object")
    ranking = []
    for model in catalog["models"]:
        score, explanation = compatibility_score(requirements, model)
        ranking.append({
            "model": model["model_id"],
            "model_name": model["model_name"],
            "score": round(score, 6),
            "score_explanation": explanation,
        })
    ranking.sort(key=lambda row: (-row["score"], row["model"]))
    strongest = sorted(
        requirements, key=lambda name: (-float(requirements[name]), name)
    )[:2]
    return {
        "selected_model": ranking[0]["model"],
        "ranking": ranking,
        "reason": "high compatibility with " + " and ".join(strongest),
        "catalog_version": catalog["catalog_version"],
        "source_type": "offline_capability_routing",
    }
